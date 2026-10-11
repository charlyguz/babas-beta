import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DEFAULT_SITES, actionDigest } from './policy';
import {
  Mutex,
  addAllowedSite,
  addBabasTab,
  clearApprovals,
  consumeApproval,
  dropApproval,
  dropApprovalsForTab,
  getAllowedSites,
  getBabasTabs,
  getCurrentTabId,
  getPairToken,
  isHalted,
  mintApproval,
  removeAllowedSite,
  removeBabasTab,
  resetStateCache,
  setCurrentTabId,
  setHalted,
  setPairToken,
} from './state';

function fakeArea() {
  const data = new Map<string, unknown>();
  const later = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));
  return {
    data,
    async get(key: string): Promise<Record<string, unknown>> {
      await later();
      return data.has(key) ? { [key]: JSON.parse(JSON.stringify(data.get(key))) } : {};
    },
    async set(items: Record<string, unknown>): Promise<void> {
      await later();
      for (const [key, value] of Object.entries(items)) data.set(key, value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
    },
  };
}

let session: ReturnType<typeof fakeArea>;
let local: ReturnType<typeof fakeArea>;

beforeEach(() => {
  session = fakeArea();
  local = fakeArea();
  vi.stubGlobal('chrome', { storage: { session, local } });
  resetStateCache();
});

const digestA = actionDigest(1, { op: 'type', target: { index: 3, snapshot_id: 's' }, text: 'ana@example.com' });
const digestB = actionDigest(1, { op: 'click', target: { index: 4, snapshot_id: 's' } });

describe('Mutex', () => {
  it('runs tasks one at a time, in order, even after a failure', async () => {
    const mutex = new Mutex();
    const order: string[] = [];
    const slow = mutex.run(async () => {
      order.push('a-start');
      await new Promise((resolve) => setTimeout(resolve, 10));
      order.push('a-end');
    });
    const failing = mutex.run(async () => {
      order.push('b');
      throw new Error('boom');
    });
    const last = mutex.run(async () => {
      order.push('c');
      return 3;
    });
    await slow;
    await expect(failing).rejects.toThrow('boom');
    expect(await last).toBe(3);
    expect(order).toEqual(['a-start', 'a-end', 'b', 'c']);
  });
});

describe('approvals', () => {
  it('mints an id that authorizes its digest exactly once', async () => {
    const id = await mintApproval(1, [digestA]);
    expect(id).toMatch(/^[0-9a-f-]{36}$/);
    expect(await consumeApproval(id, digestA)).toBe(true);
    expect(await consumeApproval(id, digestA)).toBe(false);
  });

  it('refuses an unknown id', async () => {
    await mintApproval(1, [digestA]);
    expect(await consumeApproval('forged', digestA)).toBe(false);
    expect(await consumeApproval('', digestA)).toBe(false);
  });

  it('refuses a digest that was not approved and keeps the approval usable', async () => {
    const id = await mintApproval(1, [digestA]);
    const other = actionDigest(1, { op: 'type', target: { index: 3, snapshot_id: 's' }, text: 'otro@example.com' });
    expect(await consumeApproval(id, other)).toBe(false);
    expect(await consumeApproval(id, digestB)).toBe(false);
    expect(await consumeApproval(id, digestA)).toBe(true);
  });

  it('does not let an approval for one tab authorize the same action in another', async () => {
    const id = await mintApproval(1, [actionDigest(1, { op: 'click', target: { index: 4 } })]);
    expect(await consumeApproval(id, actionDigest(2, { op: 'click', target: { index: 4 } }))).toBe(false);
    expect(await consumeApproval(id, actionDigest(1, { op: 'click', target: { index: 4 } }))).toBe(true);
  });

  it('lets a batch approval cover each of its digests once and then deletes the record', async () => {
    const id = await mintApproval(1, [digestA, digestB]);
    expect(await consumeApproval(id, digestB)).toBe(true);
    expect(await consumeApproval(id, digestB)).toBe(false);
    expect((session.data.get('approvals') as Record<string, unknown>)[id]).toBeDefined();
    expect(await consumeApproval(id, digestA)).toBe(true);
    expect((session.data.get('approvals') as Record<string, unknown>)[id]).toBeUndefined();
  });

  it('counts a repeated digest once per time it was approved', async () => {
    const id = await mintApproval(1, [digestA, digestA]);
    expect(await consumeApproval(id, digestA)).toBe(true);
    expect(await consumeApproval(id, digestA)).toBe(true);
    expect(await consumeApproval(id, digestA)).toBe(false);
  });

  it('expires after the time to live', async () => {
    const now = 1_000_000;
    const id = await mintApproval(1, [digestA], 120_000, undefined, now);
    expect(await consumeApproval(id, digestA, undefined, now + 120_001)).toBe(false);
    const fresh = await mintApproval(1, [digestA], 120_000, undefined, now);
    expect(await consumeApproval(fresh, digestA, undefined, now + 119_999)).toBe(true);
  });

  it('defaults to a 120 second life', async () => {
    const before = Date.now();
    const id = await mintApproval(1, [digestA]);
    const record = (session.data.get('approvals') as Record<string, { expiresAt: number }>)[id];
    expect(record.expiresAt).toBeGreaterThanOrEqual(before + 120_000);
    expect(record.expiresAt).toBeLessThanOrEqual(Date.now() + 120_000);
  });

  it('purges expired records when it touches the store', async () => {
    const now = 5_000_000;
    const old = await mintApproval(1, [digestA], 1000, undefined, now);
    await mintApproval(1, [digestB], 120_000, undefined, now + 5000);
    const stored = session.data.get('approvals') as Record<string, unknown>;
    expect(stored[old]).toBeUndefined();
    expect(Object.keys(stored)).toHaveLength(1);
  });

  it('lets exactly one of two parallel consumptions win', async () => {
    const id = await mintApproval(1, [digestA]);
    const results = await Promise.all([consumeApproval(id, digestA), consumeApproval(id, digestA)]);
    expect(results.filter(Boolean)).toHaveLength(1);
  });

  it('lets exactly one of many parallel consumptions win', async () => {
    const id = await mintApproval(1, [digestA]);
    const results = await Promise.all(Array.from({ length: 12 }, () => consumeApproval(id, digestA)));
    expect(results.filter(Boolean)).toHaveLength(1);
  });

  it('does not lose an update when two different digests are consumed in parallel', async () => {
    const id = await mintApproval(1, [digestA, digestB]);
    const results = await Promise.all([consumeApproval(id, digestA), consumeApproval(id, digestB)]);
    expect(results).toEqual([true, true]);
    expect(await consumeApproval(id, digestA)).toBe(false);
    expect(await consumeApproval(id, digestB)).toBe(false);
  });

  it('does not lose an approval minted while another is consumed', async () => {
    const first = await mintApproval(1, [digestA]);
    const [, second] = await Promise.all([consumeApproval(first, digestA), mintApproval(1, [digestB])]);
    expect(await consumeApproval(second, digestB)).toBe(true);
  });

  it('binds an approval to the host it was granted on when both sides know it', async () => {
    const id = await mintApproval(1, [digestA, digestB], 120_000, 'gob.mx');
    expect(await consumeApproval(id, digestA, 'evil.com')).toBe(false);
    expect(await consumeApproval(id, digestA, '')).toBe(false);
    expect(await consumeApproval(id, digestA, 'gob.mx')).toBe(true);
    expect(await consumeApproval(id, digestB)).toBe(true);
  });

  it('drops one approval, the approvals of a tab, or all of them', async () => {
    const a = await mintApproval(1, [digestA]);
    const b = await mintApproval(2, [digestA]);
    const c = await mintApproval(2, [digestB]);
    await dropApproval(a);
    expect(await consumeApproval(a, digestA)).toBe(false);
    await dropApprovalsForTab(2);
    expect(await consumeApproval(b, digestA)).toBe(false);
    expect(await consumeApproval(c, digestB)).toBe(false);
    const d = await mintApproval(1, [digestA]);
    await clearApprovals();
    expect(await consumeApproval(d, digestA)).toBe(false);
  });

  it('survives garbage in the store', async () => {
    session.data.set('approvals', { bad: 5, worse: { tabId: 'x' }, empty: { tabId: 1, digests: [], expiresAt: Date.now() + 10_000 } });
    expect(await consumeApproval('bad', digestA)).toBe(false);
    const id = await mintApproval(1, [digestA]);
    expect(await consumeApproval(id, digestA)).toBe(true);
  });

  it('keeps the store bounded', async () => {
    const ids: string[] = [];
    for (let i = 0; i < 70; i += 1) ids.push(await mintApproval(1, [`d${i}`]));
    expect(Object.keys(session.data.get('approvals') as object).length).toBeLessThanOrEqual(64);
    expect(await consumeApproval(ids[69], 'd69')).toBe(true);
  });
});

describe('halted flag', () => {
  it('is false by default, reads through to storage, and answers from memory once set', async () => {
    expect(await isHalted()).toBe(false);
    resetStateCache();
    session.data.set('halted', true);
    expect(await isHalted()).toBe(true);
  });

  it('takes effect before the write completes', async () => {
    const write = setHalted(true);
    expect(await isHalted()).toBe(true);
    await write;
    expect(session.data.get('halted')).toBe(true);
    await setHalted(false);
    expect(await isHalted()).toBe(false);
  });
});

describe('tab scope', () => {
  it('tracks the tabs BABAS may use and clears the current one when it goes', async () => {
    expect(await getBabasTabs()).toEqual([]);
    await addBabasTab(5);
    await addBabasTab(5);
    await addBabasTab(6);
    await setCurrentTabId(6);
    expect(await getBabasTabs()).toEqual([5, 6]);
    expect(await getCurrentTabId()).toBe(6);
    await removeBabasTab(5);
    expect(await getCurrentTabId()).toBe(6);
    await removeBabasTab(6);
    expect(await getBabasTabs()).toEqual([]);
    expect(await getCurrentTabId()).toBeNull();
  });

  it('does not lose tabs added in parallel', async () => {
    await Promise.all([addBabasTab(1), addBabasTab(2), addBabasTab(3)]);
    expect((await getBabasTabs()).sort()).toEqual([1, 2, 3]);
  });
});

describe('allowed sites', () => {
  it('falls back to the defaults while nothing is stored', async () => {
    expect(await getAllowedSites()).toEqual([...DEFAULT_SITES]);
  });

  it('never writes the defaults by itself', async () => {
    await getAllowedSites();
    expect(local.data.has('allowedSites')).toBe(false);
  });

  it('adds normalized hosts once and removes them again', async () => {
    expect(await addAllowedSite('https://WWW.Ejemplo.com/ruta')).toEqual([...DEFAULT_SITES, 'ejemplo.com']);
    expect(await addAllowedSite('ejemplo.com')).toEqual([...DEFAULT_SITES, 'ejemplo.com']);
    expect(await addAllowedSite('   ')).toEqual([...DEFAULT_SITES, 'ejemplo.com']);
    expect(await removeAllowedSite('www.EJEMPLO.com')).toEqual([...DEFAULT_SITES]);
    expect(await removeAllowedSite('gob.mx')).toEqual(DEFAULT_SITES.filter((s) => s !== 'gob.mx'));
  });

  it('keeps an emptied list empty instead of restoring the defaults', async () => {
    await local.set({ allowedSites: ['gob.mx'] });
    expect(await removeAllowedSite('gob.mx')).toEqual([]);
    expect(await getAllowedSites()).toEqual([]);
  });

  it('treats an unreadable value as unset and cleans readable ones', async () => {
    await local.set({ allowedSites: 'gob.mx' });
    expect(await getAllowedSites()).toEqual([...DEFAULT_SITES]);
    await local.set({ allowedSites: ['WWW.GOB.MX', 5, 'gob.mx', ''] });
    expect(await getAllowedSites()).toEqual(['gob.mx']);
  });

  it('does not lose sites added in parallel', async () => {
    await local.set({ allowedSites: [] });
    await Promise.all([addAllowedSite('a.mx'), addAllowedSite('b.mx'), addAllowedSite('c.mx')]);
    expect((await getAllowedSites()).sort()).toEqual(['a.mx', 'b.mx', 'c.mx']);
  });
});

describe('pairing token', () => {
  it('stores and reads the token', async () => {
    expect(await getPairToken()).toBeNull();
    await setPairToken('abc');
    expect(await getPairToken()).toBe('abc');
  });
});
