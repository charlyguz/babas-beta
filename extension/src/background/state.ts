import { DEFAULT_SITES, normalizeHost } from './policy';

export interface Approval {
  tabId: number;
  digests: string[];
  expiresAt: number;
  host?: string;
}

export type ApprovalMap = Record<string, Approval>;

export const APPROVAL_TTL_MS = 120_000;
const MAX_APPROVALS = 64;

export class Mutex {
  private tail: Promise<unknown> = Promise.resolve();

  run<T>(fn: () => Promise<T>): Promise<T> {
    const result = this.tail.then(fn);
    this.tail = result.catch(() => undefined);
    return result;
  }
}

const approvalLock = new Mutex();
const scopeLock = new Mutex();
const sitesLock = new Mutex();

async function sessionGet(key: string): Promise<unknown> {
  const items = await chrome.storage.session.get(key);
  return items[key];
}

async function localGet(key: string): Promise<unknown> {
  const items = await chrome.storage.local.get(key);
  return items[key];
}

// Stop must take effect before any storage round trip, so the flag is cached and written through.
let haltedCache: boolean | null = null;

export async function isHalted(): Promise<boolean> {
  if (haltedCache !== null) return haltedCache;
  const stored = (await sessionGet('halted')) === true;
  if (haltedCache === null) haltedCache = stored;
  return haltedCache;
}

export function setHalted(value: boolean): Promise<void> {
  haltedCache = value;
  return chrome.storage.session.set({ halted: value });
}

export function resetStateCache(): void {
  haltedCache = null;
}

function isApproval(v: unknown): v is Approval {
  if (typeof v !== 'object' || v === null) return false;
  const a = v as Record<string, unknown>;
  return (
    typeof a.tabId === 'number' &&
    typeof a.expiresAt === 'number' &&
    Array.isArray(a.digests) &&
    a.digests.every((d) => typeof d === 'string') &&
    (a.host === undefined || typeof a.host === 'string')
  );
}

async function readApprovals(now: number): Promise<{ map: ApprovalMap; changed: boolean }> {
  const raw = await sessionGet('approvals');
  const map: ApprovalMap = {};
  let changed = false;
  if (typeof raw === 'object' && raw !== null) {
    for (const [id, value] of Object.entries(raw as Record<string, unknown>)) {
      if (isApproval(value) && value.expiresAt > now && value.digests.length > 0) map[id] = value;
      else changed = true;
    }
  }
  return { map, changed };
}

export function mintApproval(tabId: number, digests: string[], ttlMs = APPROVAL_TTL_MS, host?: string, now = Date.now()): Promise<string> {
  return approvalLock.run(async () => {
    const { map } = await readApprovals(now);
    const ids = Object.keys(map);
    if (ids.length >= MAX_APPROVALS) {
      ids.sort((a, b) => map[a].expiresAt - map[b].expiresAt);
      for (const id of ids.slice(0, ids.length - MAX_APPROVALS + 1)) delete map[id];
    }
    const id = crypto.randomUUID();
    map[id] = { tabId, digests: [...digests], expiresAt: now + ttlMs, ...(host !== undefined ? { host } : {}) };
    await chrome.storage.session.set({ approvals: map });
    return id;
  });
}

export function consumeApproval(id: string, digest: string, host?: string, now = Date.now()): Promise<boolean> {
  return approvalLock.run(async () => {
    const { map, changed } = await readApprovals(now);
    const record = map[id];
    let granted = false;
    let dirty = changed;
    if (record && (record.host === undefined || host === undefined || record.host === host)) {
      const at = record.digests.indexOf(digest);
      if (at >= 0) {
        record.digests.splice(at, 1);
        if (record.digests.length === 0) delete map[id];
        granted = true;
        dirty = true;
      }
    }
    if (dirty) await chrome.storage.session.set({ approvals: map });
    return granted;
  });
}

export function dropApproval(id: string): Promise<void> {
  return approvalLock.run(async () => {
    const { map } = await readApprovals(Date.now());
    delete map[id];
    await chrome.storage.session.set({ approvals: map });
  });
}

export function dropApprovalsForTab(tabId: number): Promise<void> {
  return approvalLock.run(async () => {
    const { map, changed } = await readApprovals(Date.now());
    let dirty = changed;
    for (const [id, record] of Object.entries(map)) {
      if (record.tabId === tabId) {
        delete map[id];
        dirty = true;
      }
    }
    if (dirty) await chrome.storage.session.set({ approvals: map });
  });
}

export function clearApprovals(): Promise<void> {
  return approvalLock.run(() => chrome.storage.session.set({ approvals: {} }));
}

export async function getBabasTabs(): Promise<number[]> {
  const raw = await sessionGet('babasTabs');
  return Array.isArray(raw) ? raw.filter((id): id is number => typeof id === 'number') : [];
}

export function addBabasTab(tabId: number): Promise<void> {
  return scopeLock.run(async () => {
    const tabs = await getBabasTabs();
    if (!tabs.includes(tabId)) await chrome.storage.session.set({ babasTabs: [...tabs, tabId] });
  });
}

export function removeBabasTab(tabId: number): Promise<void> {
  return scopeLock.run(async () => {
    const tabs = await getBabasTabs();
    if (tabs.includes(tabId)) await chrome.storage.session.set({ babasTabs: tabs.filter((id) => id !== tabId) });
    if ((await getCurrentTabId()) === tabId) await chrome.storage.session.set({ currentTabId: null });
  });
}

export async function isBabasTab(tabId: number): Promise<boolean> {
  return (await getBabasTabs()).includes(tabId);
}

export async function getCurrentTabId(): Promise<number | null> {
  const raw = await sessionGet('currentTabId');
  return typeof raw === 'number' ? raw : null;
}

export function setCurrentTabId(tabId: number | null): Promise<void> {
  return scopeLock.run(() => chrome.storage.session.set({ currentTabId: tabId }));
}

export async function getGroupId(): Promise<number | null> {
  const raw = await sessionGet('groupId');
  return typeof raw === 'number' ? raw : null;
}

export function setGroupId(groupId: number | null): Promise<void> {
  return scopeLock.run(() => chrome.storage.session.set({ groupId }));
}

function cleanSites(raw: unknown): string[] | null {
  if (!Array.isArray(raw)) return null;
  const sites = raw.filter((s): s is string => typeof s === 'string').map(normalizeHost).filter((s) => s !== '');
  return [...new Set(sites)];
}

// Nothing is written at install: a get-then-set there can overwrite a list stored in the same instant, so the defaults apply while the key is unset.
export async function getAllowedSites(): Promise<string[]> {
  return cleanSites(await localGet('allowedSites')) ?? [...DEFAULT_SITES];
}

export function addAllowedSite(host: string): Promise<string[]> {
  return sitesLock.run(async () => {
    const normalized = normalizeHost(host);
    const sites = await getAllowedSites();
    if (normalized && !sites.includes(normalized)) sites.push(normalized);
    await chrome.storage.local.set({ allowedSites: sites });
    return sites;
  });
}

export function removeAllowedSite(host: string): Promise<string[]> {
  return sitesLock.run(async () => {
    const normalized = normalizeHost(host);
    const sites = (await getAllowedSites()).filter((s) => s !== normalized);
    await chrome.storage.local.set({ allowedSites: sites });
    return sites;
  });
}

export async function getPairToken(): Promise<string | null> {
  const raw = await localGet('pairToken');
  return typeof raw === 'string' && raw.length > 0 ? raw : null;
}

export function setPairToken(token: string): Promise<void> {
  return chrome.storage.local.set({ pairToken: token });
}

export function setBridgePort(port: number): Promise<void> {
  return chrome.storage.local.set({ bridgePort: port });
}
