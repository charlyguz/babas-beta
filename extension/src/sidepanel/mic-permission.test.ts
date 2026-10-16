import { describe, expect, it, vi } from 'vitest';
import { openMicPermissionTab, watchMicrophonePermission, type PermissionStatusLike, type TabLike, type TabsApi, type WindowsApi } from './mic-permission';

const URL = 'chrome-extension://abc/micpermission.html';
const flush = async (): Promise<void> => {
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
};

function fakeTabs(existing: TabLike[] = []): { tabs: TabsApi; windows: WindowsApi; calls: string[] } {
  const calls: string[] = [];
  const tabs: TabsApi = {
    query: vi.fn(async (info) => {
      calls.push(`query ${info.url}`);
      return existing;
    }),
    update: vi.fn(async (id, properties) => {
      calls.push(`update ${id} ${JSON.stringify(properties)}`);
    }),
    create: vi.fn(async (properties) => {
      calls.push(`create ${properties.url}`);
    }),
  };
  const windows: WindowsApi = {
    update: vi.fn(async (id, info) => {
      calls.push(`window ${id} ${JSON.stringify(info)}`);
    }),
  };
  return { tabs, windows, calls };
}

describe('openMicPermissionTab', () => {
  it('opens the page once when no tab has it yet', async () => {
    const { tabs, windows, calls } = fakeTabs();
    await openMicPermissionTab(URL, tabs, windows);
    expect(calls).toEqual([`query ${URL}`, `create ${URL}`]);
  });

  it('focuses the tab that is already open instead of opening another one', async () => {
    const { tabs, windows, calls } = fakeTabs([{ id: 7, windowId: 3 }]);
    await openMicPermissionTab(URL, tabs, windows);
    await openMicPermissionTab(URL, tabs, windows);
    expect(calls).toEqual([`query ${URL}`, 'update 7 {"active":true}', 'window 3 {"focused":true}', `query ${URL}`, 'update 7 {"active":true}', 'window 3 {"focused":true}']);
    expect(tabs.create).not.toHaveBeenCalled();
  });

  it('opens a new tab when the old one vanished before it could be focused', async () => {
    const { tabs, windows, calls } = fakeTabs([{ id: 7, windowId: 3 }]);
    vi.mocked(tabs.update).mockRejectedValueOnce(new Error('No tab with id: 7'));
    await openMicPermissionTab(URL, tabs, windows);
    expect(calls).toContain(`create ${URL}`);
  });

  it('opens a new tab when looking for the old one fails, and does not throw when that fails too', async () => {
    const { tabs, windows, calls } = fakeTabs();
    vi.mocked(tabs.query).mockRejectedValueOnce(new Error('boom'));
    await openMicPermissionTab(URL, tabs, windows);
    expect(calls).toEqual([`create ${URL}`]);
    vi.mocked(tabs.query).mockRejectedValueOnce(new Error('boom'));
    vi.mocked(tabs.create).mockRejectedValueOnce(new Error('no'));
    await expect(openMicPermissionTab(URL, tabs, windows)).resolves.toBeUndefined();
  });

  it('still counts a focused tab when the window cannot be raised', async () => {
    const { tabs, windows } = fakeTabs([{ id: 7, windowId: 3 }]);
    vi.mocked(windows.update).mockRejectedValueOnce(new Error('no window'));
    await openMicPermissionTab(URL, tabs, windows);
    expect(tabs.create).not.toHaveBeenCalled();
  });

  it('skips a tab without an id', async () => {
    const { tabs, windows } = fakeTabs([{}, { id: 9 }]);
    await openMicPermissionTab(URL, tabs, windows);
    expect(tabs.update).toHaveBeenCalledTimes(1);
    expect(tabs.update).toHaveBeenCalledWith(9, { active: true });
    expect(tabs.create).not.toHaveBeenCalled();
  });
});

class FakeStatus implements PermissionStatusLike {
  listeners = new Set<() => void>();
  constructor(public state: string) {}
  addEventListener(_type: 'change', listener: () => void): void {
    this.listeners.add(listener);
  }
  removeEventListener(_type: 'change', listener: () => void): void {
    this.listeners.delete(listener);
  }
  change(state: string): void {
    this.state = state;
    for (const listener of [...this.listeners]) listener();
  }
}

function watchHarness(initial: string): { status: FakeStatus; granted: ReturnType<typeof vi.fn>; resume: () => void; resumeListeners: Set<() => void>; stop: () => void } {
  const status = new FakeStatus(initial);
  const granted = vi.fn();
  const resumeListeners = new Set<() => void>();
  const stop = watchMicrophonePermission(granted, {
    query: async () => status,
    onResume: (listener) => {
      resumeListeners.add(listener);
      return () => resumeListeners.delete(listener);
    },
  });
  return { status, granted, stop, resumeListeners, resume: () => [...resumeListeners].forEach((listener) => listener()) };
}

describe('watchMicrophonePermission', () => {
  it('reports once when the permission turns to granted after the watch began, then lets go of everything', async () => {
    const w = watchHarness('denied');
    await flush();
    expect(w.granted).not.toHaveBeenCalled();
    w.status.change('prompt');
    expect(w.granted).not.toHaveBeenCalled();
    w.status.change('granted');
    expect(w.granted).toHaveBeenCalledTimes(1);
    w.status.change('denied');
    w.status.change('granted');
    expect(w.granted).toHaveBeenCalledTimes(1);
    expect(w.status.listeners.size).toBe(0);
    expect(w.resumeListeners.size).toBe(0);
  });

  it('does not report a permission that was already granted when the watch began', async () => {
    const w = watchHarness('granted');
    await flush();
    w.status.change('granted');
    w.resume();
    expect(w.granted).not.toHaveBeenCalled();
    w.status.change('prompt');
    w.status.change('granted');
    expect(w.granted).toHaveBeenCalledTimes(1);
  });

  it('checks again when the panel comes back to the front', async () => {
    const w = watchHarness('prompt');
    await flush();
    w.status.state = 'granted';
    w.resume();
    expect(w.granted).toHaveBeenCalledTimes(1);
  });

  it('stops listening when told to, even before the browser answered', async () => {
    const early = watchHarness('prompt');
    early.stop();
    await flush();
    expect(early.status.listeners.size).toBe(0);
    expect(early.resumeListeners.size).toBe(0);
    early.status.change('granted');
    expect(early.granted).not.toHaveBeenCalled();

    const late = watchHarness('prompt');
    await flush();
    late.stop();
    late.stop();
    late.status.change('granted');
    expect(late.granted).not.toHaveBeenCalled();
    expect(late.status.listeners.size).toBe(0);
    expect(late.resumeListeners.size).toBe(0);
  });

  it('does nothing when the browser cannot tell', async () => {
    const granted = vi.fn();
    const onResume = vi.fn(() => () => undefined);
    watchMicrophonePermission(granted, { query: () => Promise.reject(new Error('unsupported')), onResume });
    watchMicrophonePermission(granted, {
      query: () => {
        throw new TypeError('no permissions');
      },
      onResume,
    });
    await flush();
    expect(granted).not.toHaveBeenCalled();
    expect(onResume).not.toHaveBeenCalled();
  });
});
