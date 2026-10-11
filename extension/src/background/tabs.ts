import { BabasError, type PageCommand, type PageResponse } from '../shared/protocol';

const POLL_MS = 100;

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function withTimeout<T>(promise: Promise<T>, ms: number, what: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new BabasError('TIMEOUT', `${what} took longer than ${Math.round(ms / 1000)} s`)), ms);
  });
  try {
    return await Promise.race([promise, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

export async function getTab(tabId: number): Promise<chrome.tabs.Tab> {
  try {
    return await chrome.tabs.get(tabId);
  } catch {
    throw new BabasError('TAB_NOT_FOUND', `Tab ${tabId} no longer exists`);
  }
}

export async function waitForComplete(tabId: number, timeoutMs = 30_000): Promise<chrome.tabs.Tab> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const tab = await getTab(tabId);
    if (tab.status === 'complete') return tab;
    if (Date.now() > deadline) throw new BabasError('TIMEOUT', 'The page did not finish loading');
    await sleep(POLL_MS);
  }
}

function isUnscriptable(url: string | undefined): boolean {
  if (!url) return false;
  return /^(chrome|edge|brave|about|devtools|view-source|chrome-extension|chrome-search):/i.test(url) || url.startsWith('https://chromewebstore.google.com');
}

async function sendRaw(tabId: number, cmd: PageCommand): Promise<PageResponse | undefined> {
  return (await chrome.tabs.sendMessage(tabId, { babas: cmd })) as PageResponse | undefined;
}

export async function ensureInjected(tabId: number): Promise<void> {
  try {
    const pong = await sendRaw(tabId, { op: 'ping' });
    if (pong && pong.ok) return;
  } catch {
    // no content script yet
  }
  const tab = await getTab(tabId);
  if (isUnscriptable(tab.url)) {
    throw new BabasError('UNSCRIPTABLE', `BABAS cannot work on this page (${tab.url}). Open a website first.`, { url: tab.url });
  }
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] });
  } catch (err) {
    throw new BabasError('UNSCRIPTABLE', `BABAS cannot work on this page: ${err instanceof Error ? err.message : String(err)}`, {
      url: tab.url,
    });
  }
}

export async function pageCommand(tabId: number, cmd: PageCommand, timeoutMs = 15_000): Promise<unknown> {
  await waitForComplete(tabId, 30_000);
  await ensureInjected(tabId);
  let response: PageResponse | undefined;
  try {
    response = await withTimeout(sendRaw(tabId, cmd), timeoutMs, `Page operation "${cmd.op}"`);
  } catch (err) {
    if (err instanceof BabasError) throw err;
    const message = err instanceof Error ? err.message : String(err);
    if (/message port closed|receiving end does not exist|back\/forward cache/i.test(message) && ['click', 'press', 'select'].includes(cmd.op)) {
      return { navigated: true };
    }
    throw new BabasError('INTERNAL', `Page did not answer: ${message}`);
  }
  if (!response) throw new BabasError('INTERNAL', 'Page did not answer');
  if (!response.ok) throw new BabasError(response.error.code, response.error.message, response.error.data);
  return response.result;
}

export async function settle(tabId: number): Promise<void> {
  await sleep(250);
  try {
    const tab = await getTab(tabId);
    if (tab.status !== 'complete') await waitForComplete(tabId, 30_000);
    await pageCommand(tabId, { op: 'idle', quiet_ms: 300, timeout_ms: 2000 }, 5000);
  } catch {
    // best effort: the next observation reports the real state
  }
}

export async function navigate(tabId: number, url: string, timeoutMs = 45_000): Promise<chrome.tabs.Tab> {
  const before = await getTab(tabId);
  await chrome.tabs.update(tabId, { url });
  const started = Date.now();
  while (Date.now() - started < 3000) {
    const tab = await getTab(tabId);
    if (tab.status === 'loading' || tab.url !== before.url || tab.pendingUrl) break;
    await sleep(POLL_MS);
  }
  const tab = await waitForComplete(tabId, timeoutMs);
  if (!isUnscriptable(tab.url)) {
    try {
      await pageCommand(tabId, { op: 'idle', quiet_ms: 300, timeout_ms: 2500 }, 5000);
    } catch {
      // the page may not accept scripts; observation will say so
    }
  }
  return getTab(tabId);
}

export function hostOf(url: string | undefined): string {
  if (!url) return '';
  try {
    return new URL(url).hostname.toLowerCase().replace(/^www\./, '');
  } catch {
    return '';
  }
}
