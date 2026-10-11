import { BabasError, isTarget, type PageCommand, type Target } from '../shared/protocol';
import { Bridge } from './bridge';
import { getTab, navigate, pageCommand, settle } from './tabs';

const bridge = new Bridge();
let currentTabId: number | null = null;

function asTarget(value: unknown): Target {
  if (!isTarget(value)) throw new BabasError('BAD_REQUEST', 'A valid target (index, selector or text) is required');
  return value;
}

function optionalTarget(value: unknown): Target | undefined {
  return value === undefined || value === null ? undefined : asTarget(value);
}

async function workingTabId(): Promise<number> {
  if (currentTabId !== null) {
    try {
      await getTab(currentTabId);
      return currentTabId;
    } catch {
      currentTabId = null;
    }
  }
  const tab = await pickWorkingTab();
  if (!tab || tab.id === undefined) throw new BabasError('TAB_NOT_FOUND', 'There is no active tab to work on');
  currentTabId = tab.id;
  return tab.id;
}

async function pickWorkingTab(): Promise<chrome.tabs.Tab | undefined> {
  const [focused] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (focused?.id !== undefined) return focused;
  const [anyActive] = await chrome.tabs.query({ active: true });
  if (anyActive?.id !== undefined) return anyActive;
  const webTabs = (await chrome.tabs.query({})).filter((t) => t.id !== undefined && /^https?:\/\//i.test(t.url ?? ''));
  webTabs.sort((a, b) => (b.lastAccessed ?? 0) - (a.lastAccessed ?? 0));
  return webTabs[0];
}

async function tabSummary(tabId: number): Promise<Record<string, unknown>> {
  const tab = await getTab(tabId);
  return { tab_id: tabId, url: tab.url ?? '', title: tab.title ?? '' };
}

function buildActCommand(payload: Record<string, unknown>, authorized: boolean): PageCommand {
  const op = String(payload.op ?? '');
  switch (op) {
    case 'click':
      return { op: 'click', target: asTarget(payload.target), authorized };
    case 'type':
      return { op: 'type', target: asTarget(payload.target), text: String(payload.text ?? ''), authorized };
    case 'press':
      return { op: 'press', key: String(payload.key ?? 'Enter'), target: optionalTarget(payload.target), authorized };
    case 'select':
      return { op: 'select', target: asTarget(payload.target), value: String(payload.value ?? ''), authorized };
    case 'scroll':
      return { op: 'scroll', direction: payload.direction === 'up' ? 'up' : 'down', amount: Number(payload.amount) || 500 };
    case 'read':
      return { op: 'read', target: optionalTarget(payload.target), max_chars: Number(payload.max_chars) || 6000 };
    case 'highlight': {
      const mode = payload.mode === 'off' ? 'off' : payload.mode === 'target' ? 'target' : 'all';
      return { op: 'highlight', mode, target: optionalTarget(payload.target) };
    }
    default:
      throw new BabasError('BAD_REQUEST', `Unknown action op: ${op}`);
  }
}

bridge.on('page.state', async (payload) => {
  const tabId = await workingTabId();
  const state = (await pageCommand(tabId, {
    op: 'snapshot',
    highlight: payload.highlight === true,
    max_elements: Number(payload.max_elements) || 250,
    max_text: Number(payload.max_text) || 2000,
  })) as Record<string, unknown>;
  return { ...state, tab_id: tabId };
});

bridge.on('page.describe', async (payload) => {
  const tabId = await workingTabId();
  return (await pageCommand(tabId, { op: 'describe', target: asTarget(payload.target) })) as Record<string, unknown>;
});

bridge.on('page.exists', async (payload) => {
  const tabId = await workingTabId();
  return (await pageCommand(tabId, { op: 'exists', target: asTarget(payload.target) })) as Record<string, unknown>;
});

bridge.on('page.act', async (payload) => {
  const tabId = await workingTabId();
  const cmd = buildActCommand(payload, false);
  const result = await pageCommand(tabId, cmd);
  if (cmd.op === 'click' || cmd.op === 'press' || cmd.op === 'select') await settle(tabId);
  return { result: (result ?? {}) as Record<string, unknown>, ...(await tabSummary(tabId)) };
});

bridge.on('tab.goto', async (payload) => {
  const url = String(payload.url ?? '');
  if (!/^https?:\/\//i.test(url)) throw new BabasError('BAD_REQUEST', `Only http(s) URLs can be opened: ${url}`);
  const tabId = await workingTabId();
  await navigate(tabId, url);
  return tabSummary(tabId);
});

bridge.on('tab.info', async () => tabSummary(await workingTabId()));

bridge.on('tab.new', async (payload) => {
  const url = typeof payload.url === 'string' && /^https?:\/\//i.test(payload.url) ? payload.url : undefined;
  const tab = await chrome.tabs.create({ url, active: true });
  if (tab.id === undefined) throw new BabasError('INTERNAL', 'Could not create a tab');
  currentTabId = tab.id;
  if (url) await navigate(tab.id, url);
  return tabSummary(tab.id);
});

bridge.on('tab.switch', async (payload) => {
  const tabId = Number(payload.tab_id);
  const tab = await getTab(tabId);
  await chrome.tabs.update(tabId, { active: true });
  if (tab.windowId !== undefined) await chrome.windows.update(tab.windowId, { focused: true });
  currentTabId = tabId;
  return tabSummary(tabId);
});

bridge.on('tab.close', async (payload) => {
  const tabId = payload.tab_id === undefined ? await workingTabId() : Number(payload.tab_id);
  await getTab(tabId);
  await chrome.tabs.remove(tabId);
  if (currentTabId === tabId) currentTabId = null;
  return { closed: tabId };
});

bridge.on('tab.screenshot', async () => {
  const tabId = await workingTabId();
  const tab = await getTab(tabId);
  await chrome.tabs.update(tabId, { active: true });
  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'png' });
  return { png_base64: dataUrl.replace(/^data:image\/png;base64,/, '') };
});

chrome.tabs.onRemoved.addListener((tabId) => {
  if (currentTabId === tabId) currentTabId = null;
});

chrome.runtime.onInstalled.addListener(() => void bridge.connect());
chrome.runtime.onStartup.addListener(() => void bridge.connect());
chrome.alarms.create('babas-reconnect', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'babas-reconnect' && !bridge.connected) void bridge.connect();
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes.bridgePort) void bridge.reconnect();
});

void bridge.connect();
