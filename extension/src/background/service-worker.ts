import { VOICE_ERROR_KEYS, type LogKind, type PanelToSw, type RunStateName, type SwToPanel } from '../shared/messages';
import { BabasError, isTarget, type PageCommand, type Target } from '../shared/protocol';
import { Bridge, getBridgePort } from './bridge';
import {
  actionDigest,
  confirmLine,
  confirmTitle,
  hostAllowed,
  hostCheckFor,
  isHttpUrl,
  normalizeHost,
  parseConfirmPayload,
  plausibleSiteHost,
  timeoutMsFrom,
  type ConfirmAction,
  type Translate,
} from './policy';
import { adoptForTask, adoptTab, ownedTab, resolveWorkingTab } from './scope';
import {
  APPROVAL_TTL_MS,
  addAllowedSite,
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
  setBridgePort,
  setCurrentTabId,
  setHalted,
  setPairToken,
} from './state';
import { clearHighlights, getTab, navigate, pageCommand, settle, withTimeout, type PrepareCommand } from './tabs';
import * as ui from './ui';
import { VoiceRelay } from './voice-relay';

const bridge = new Bridge();
const t: Translate = (key, substitutions) => chrome.i18n.getMessage(key, substitutions) || key;
const voice = new VoiceRelay<chrome.runtime.Port>({
  isBridgeUp: () => bridge.connected,
  emit: (type, payload) => bridge.emit(type, payload),
  send: (port, message) => ui.sendTo(port, message),
  text: (code) => t(VOICE_ERROR_KEYS[code]),
});
const GATED_OPS = new Set(['click', 'type', 'press', 'select']);
const LOG_KINDS: readonly string[] = ['user', 'assistant', 'step', 'result', 'error', 'info'];
const RUN_STATES: readonly string[] = ['running', 'done', 'failed', 'stopped'];
const MAX_LOG_TEXT = 20_000;

let running = false;

function asTarget(value: unknown): Target {
  if (!isTarget(value)) throw new BabasError('BAD_REQUEST', 'A valid target (index, selector or text) is required');
  return value;
}

function optionalTarget(value: unknown): Target | undefined {
  return value === undefined || value === null ? undefined : asTarget(value);
}

function requireString(value: unknown, what: string, max = 4000): string {
  if (typeof value !== 'string' || value.length > max) throw new BabasError('BAD_REQUEST', `${what} must be a string of at most ${max} characters`);
  return value;
}

function tabSummary(tab: chrome.tabs.Tab): Record<string, unknown> {
  return { tab_id: tab.id, url: tab.url ?? '', title: tab.title ?? '' };
}

async function tabSummaryById(tabId: number): Promise<Record<string, unknown>> {
  return tabSummary(await getTab(tabId));
}

function tabHost(tab: chrome.tabs.Tab): string {
  const check = hostCheckFor(tab.url || tab.pendingUrl);
  return check.check ? check.host : '';
}

function domainNotAllowed(host: string, url: string): BabasError {
  return new BabasError('DOMAIN_NOT_ALLOWED', `The site ${host || url} is not on the allowed list. Ask the user to allow it first.`, { host, url });
}

async function assertRunnable(): Promise<void> {
  if (await isHalted()) throw new BabasError('HALTED', 'The user pressed Stop. Nothing else will be done in the browser.');
}

async function assertTabAllowed(tab: chrome.tabs.Tab): Promise<void> {
  const url = tab.url || tab.pendingUrl;
  const check = hostCheckFor(url);
  if (check.check && !hostAllowed(check.host, await getAllowedSites())) throw domainNotAllowed(check.host, url ?? '');
}

async function assertTargetUrlAllowed(url: string): Promise<void> {
  const host = normalizeHost(url);
  if (!hostAllowed(host, await getAllowedSites())) throw domainNotAllowed(host, url);
}

async function pageTabId(): Promise<number> {
  await assertRunnable();
  const tab = await resolveWorkingTab();
  await assertTabAllowed(tab);
  return tab.id as number;
}

const lateCheck: PrepareCommand = async (tab) => {
  await assertRunnable();
  await assertTabAllowed(tab);
};

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
  const tabId = await pageTabId();
  const state = (await pageCommand(
    tabId,
    {
      op: 'snapshot',
      highlight: payload.highlight === true,
      max_elements: Number(payload.max_elements) || 250,
      max_text: Number(payload.max_text) || 2000,
    },
    15_000,
    lateCheck,
  )) as Record<string, unknown>;
  return { ...state, tab_id: tabId };
});

bridge.on('page.describe', async (payload) => {
  const tabId = await pageTabId();
  return (await pageCommand(tabId, { op: 'describe', target: asTarget(payload.target) }, 15_000, lateCheck)) as Record<string, unknown>;
});

bridge.on('page.exists', async (payload) => {
  const tabId = await pageTabId();
  return (await pageCommand(tabId, { op: 'exists', target: asTarget(payload.target) }, 15_000, lateCheck)) as Record<string, unknown>;
});

bridge.on('page.act', async (payload) => {
  const tabId = await pageTabId();
  const first = buildActCommand(payload, false);
  const confirmationId = typeof payload.confirmation_id === 'string' && payload.confirmation_id.length <= 128 ? payload.confirmation_id : '';
  const gated = GATED_OPS.has(first.op) && confirmationId !== '';
  const result = await pageCommand(tabId, first, 15_000, async (tab) => {
    await lateCheck(tab);
    if (!gated) return undefined;
    const authorized = await consumeApproval(confirmationId, actionDigest(tabId, payload), tabHost(tab));
    await assertRunnable();
    return authorized ? buildActCommand(payload, true) : undefined;
  });
  if (first.op === 'click' || first.op === 'press' || first.op === 'select') await settle(tabId);
  return { result: (result ?? {}) as Record<string, unknown>, ...(await tabSummaryById(tabId)) };
});

bridge.on('tab.goto', async (payload) => {
  await assertRunnable();
  const url = requireString(payload.url, 'url', 8000);
  if (!isHttpUrl(url)) throw new BabasError('BAD_REQUEST', `Only http(s) URLs can be opened: ${url}`);
  const tab = await resolveWorkingTab();
  await assertTargetUrlAllowed(url);
  await navigate(tab.id as number, url);
  return tabSummaryById(tab.id as number);
});

bridge.on('tab.info', async () => {
  await assertRunnable();
  return tabSummary(await resolveWorkingTab());
});

bridge.on('tab.new', async (payload) => {
  await assertRunnable();
  let url: string | undefined;
  if (payload.url !== undefined && payload.url !== null && payload.url !== '') {
    url = requireString(payload.url, 'url', 8000);
    if (!isHttpUrl(url)) throw new BabasError('BAD_REQUEST', `Only http(s) URLs can be opened: ${url}`);
    await assertTargetUrlAllowed(url);
  }
  const tab = await chrome.tabs.create({ url: 'about:blank', active: true });
  if (tab.id === undefined) throw new BabasError('INTERNAL', 'Could not create a tab');
  await adoptTab(tab.id);
  if (url) await navigate(tab.id, url);
  return tabSummaryById(tab.id);
});

bridge.on('tab.switch', async (payload) => {
  await assertRunnable();
  const tabId = Number(payload.tab_id);
  if (!Number.isInteger(tabId)) throw new BabasError('BAD_REQUEST', 'tab_id must be an integer');
  const tab = await ownedTab(tabId);
  await chrome.tabs.update(tabId, { active: true });
  if (tab.windowId !== undefined) await chrome.windows.update(tab.windowId, { focused: true });
  await setCurrentTabId(tabId);
  return tabSummaryById(tabId);
});

bridge.on('tab.close', async (payload) => {
  await assertRunnable();
  const explicit = payload.tab_id !== undefined && payload.tab_id !== null;
  const tabId = explicit ? Number(payload.tab_id) : await getCurrentTabId();
  if (tabId === null || !Number.isInteger(tabId)) throw new BabasError('TAB_NOT_FOUND', 'There is no BABAS tab to close');
  await ownedTab(tabId);
  await chrome.tabs.remove(tabId);
  await removeBabasTab(tabId);
  return { closed: tabId };
});

bridge.on('tab.screenshot', async () => {
  const tabId = await pageTabId();
  await chrome.tabs.update(tabId, { active: true });
  const tab = await getTab(tabId);
  await assertRunnable();
  await assertTabAllowed(tab);
  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'png' });
  return { png_base64: dataUrl.replace(/^data:image\/png;base64,/, '') };
});

function elementLabel(info: unknown): string {
  const element = (info as { element?: { label?: unknown; text?: unknown } } | null)?.element;
  const label = typeof element?.label === 'string' && element.label.trim() ? element.label : element?.text;
  return typeof label === 'string' ? label.replace(/\s+/g, ' ').trim().slice(0, 200) : '';
}

async function describeLine(tabId: number, action: ConfirmAction, host: string): Promise<string> {
  const target = action.target ?? (action.op === 'press' ? { selector: ':focus' } : undefined);
  let label = '';
  if (target) {
    try {
      const info = await withTimeout(pageCommand(tabId, { op: 'describe', target }, 5000, lateCheck), 8000, 'Describing the target');
      label = elementLabel(info);
    } catch {
      label = '';
    }
  }
  return confirmLine(action.op, label, host, { text: action.text, value: action.value, key: action.key }, t);
}

const STOPPED_CONFIRM = { approved: false, reason: 'stopped' };

bridge.on('ui.confirm', async (payload) => {
  const request = parseConfirmPayload(payload);
  const timeoutMs = timeoutMsFrom(payload.timeout_s, 300);
  if (await isHalted()) return STOPPED_CONFIRM;
  const tab = await resolveWorkingTab();
  const tabId = tab.id as number;
  const host = tabHost(tab);
  const lines = await Promise.all(request.actions.map((action) => describeLine(tabId, action, host)));
  if (await isHalted()) return STOPPED_CONFIRM;
  const resolution = await ui.openRequest(
    'confirm',
    { title: confirmTitle(request.reasons, t), lines, explanation: request.explanation },
    timeoutMs,
  );
  if (!resolution.approved) return { approved: false, reason: resolution.reason === 'timeout' || resolution.reason === 'stopped' ? resolution.reason : 'denied' };
  const confirmationId = await mintApproval(
    tabId,
    request.actions.map((action) => actionDigest(tabId, action)),
    APPROVAL_TTL_MS,
    host,
  );
  // Stop sets the flag before it clears approvals, so checking after minting leaves no approval alive across a Stop.
  if (await isHalted()) {
    await dropApproval(confirmationId);
    return STOPPED_CONFIRM;
  }
  return { approved: true, confirmation_id: confirmationId };
});

bridge.on('ui.ask', async (payload) => {
  const question = requireString(payload.question, 'question');
  if (payload.kind !== undefined && payload.kind !== 'text' && payload.kind !== 'handoff') {
    throw new BabasError('BAD_REQUEST', 'kind must be "text" or "handoff"');
  }
  const kind = payload.kind === 'handoff' ? 'handoff' : 'text';
  const timeoutMs = timeoutMsFrom(payload.timeout_s, 600);
  if (await isHalted()) return { answer: null, reason: 'stopped' };
  const resolution = await ui.openRequest('ask', { question, kind }, timeoutMs);
  if (resolution.answer !== null) return { answer: resolution.answer };
  return { answer: null, reason: resolution.reason === 'timeout' || resolution.reason === 'stopped' ? resolution.reason : 'cancelled' };
});

bridge.on('ui.allow_site', async (payload) => {
  const host = normalizeHost(requireString(payload.host, 'host', 300));
  if (!host || !plausibleSiteHost(host)) throw new BabasError('BAD_REQUEST', 'host must be a complete site name such as example.com');
  const url = typeof payload.url === 'string' ? payload.url.slice(0, 2000) : '';
  const timeoutMs = timeoutMsFrom(payload.timeout_s, 300);
  if (await isHalted()) return STOPPED_CONFIRM;
  const resolution = await ui.openRequest('allow', { host, url }, timeoutMs);
  if (!resolution.approved) return { approved: false, reason: resolution.reason === 'timeout' || resolution.reason === 'stopped' ? resolution.reason : 'denied' };
  if (await isHalted()) return STOPPED_CONFIRM;
  await addAllowedSite(host);
  void broadcastSettings();
  return { approved: true };
});

bridge.on('ui.transcript', async (payload) => voice.handleTranscript(payload));

bridge.on('ui.log', async (payload) => {
  const kind = String(payload.kind);
  if (!LOG_KINDS.includes(kind)) throw new BabasError('BAD_REQUEST', `Unknown log kind: ${kind}`);
  const text = requireString(payload.text, 'text', MAX_LOG_TEXT * 5).slice(0, MAX_LOG_TEXT);
  ui.log(kind as LogKind, text, typeof payload.ok === 'boolean' ? payload.ok : undefined);
  return {};
});

bridge.on('ui.run_state', async (payload) => {
  const state = String(payload.state);
  if (!RUN_STATES.includes(state)) throw new BabasError('BAD_REQUEST', `Unknown run state: ${state}`);
  const summary = payload.summary === undefined || payload.summary === null ? undefined : requireString(payload.summary, 'summary', MAX_LOG_TEXT * 5).slice(0, MAX_LOG_TEXT);
  if (state === 'running' && (await isHalted())) return {};
  running = state === 'running';
  ui.broadcast({ type: 'run_state', state: state as RunStateName, ...(summary === undefined ? {} : { summary }) });
  await broadcastStatus();
  return {};
});

async function statusMessage(): Promise<SwToPanel> {
  return { type: 'status', connected: bridge.connected, halted: await isHalted(), running };
}

async function settingsMessage(): Promise<SwToPanel> {
  return { type: 'settings', bridgePort: await getBridgePort(), sites: await getAllowedSites() };
}

async function broadcastStatus(): Promise<void> {
  ui.broadcast(await statusMessage());
}

async function broadcastSettings(): Promise<void> {
  ui.broadcast(await settingsMessage());
}

async function stopRun(): Promise<void> {
  const persisted = setHalted(true);
  running = false;
  ui.closeAll('stopped');
  ui.broadcast({ type: 'run_state', state: 'stopped' });
  bridge.emit('run.stop', {});
  await persisted.catch(() => undefined);
  await clearApprovals().catch(() => undefined);
  await broadcastStatus();
  for (const tabId of await getBabasTabs()) void clearHighlights(tabId);
}

async function startTask(text: string, windowId: number | undefined): Promise<void> {
  await setHalted(false);
  try {
    await adoptForTask(windowId);
  } catch {
    ui.log('error', t('runFailed'));
    await broadcastStatus();
    return;
  }
  if (bridge.connected) bridge.emit('task.start', { text });
  else ui.log('error', t('notConnectedHelp'));
  await broadcastStatus();
}

async function onPanelMessage(message: PanelToSw, port: chrome.runtime.Port): Promise<void> {
  switch (message.type) {
    case 'task.start':
      await startTask(message.text, message.windowId);
      return;
    case 'stop':
      await stopRun();
      return;
    case 'resume':
      await setHalted(false);
      await broadcastStatus();
      return;
    case 'settings.get':
      ui.sendTo(port, await settingsMessage());
      return;
    case 'voice.audio':
      voice.request(port, message);
      return;
    case 'settings.set':
      if (message.bridgePort !== undefined) await setBridgePort(message.bridgePort);
      if (message.removeSite !== undefined) await removeAllowedSite(message.removeSite);
      await broadcastSettings();
      return;
    default:
  }
}

ui.initPanels({
  onMessage: (message, port) => {
    onPanelMessage(message, port).catch(() => {
      if (message.type === 'task.start') ui.log('error', t('runFailed'));
    });
  },
  onDisconnect: (port) => voice.portClosed(port),
  greeting: async () => [await statusMessage(), await settingsMessage()],
});

bridge.setHelloExtras(async () => {
  const token = await getPairToken();
  return token ? { token } : {};
});
bridge.onWelcome((payload) => {
  if (typeof payload.token === 'string' && payload.token.length > 0 && payload.token.length <= 256) void setPairToken(payload.token);
});
bridge.onStatus((connected) => {
  if (!connected) {
    ui.closeAll('stopped');
    voice.bridgeDown();
  }
  void broadcastStatus();
});

chrome.tabs.onRemoved.addListener((tabId) => {
  void removeBabasTab(tabId);
  void dropApprovalsForTab(tabId);
});

chrome.runtime.onInstalled.addListener(() => void bridge.connect());
chrome.runtime.onStartup.addListener(() => void bridge.connect());
chrome.alarms.create('babas-reconnect', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'babas-reconnect' && !bridge.connected) void bridge.connect();
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local') return;
  if (changes.bridgePort) void bridge.reconnect();
  if (changes.allowedSites || changes.bridgePort) void broadcastSettings();
});

try {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => undefined);
} catch {
  // the side panel API is missing on very old browsers; the action button then does nothing
}
ui.refreshBadge();
void bridge.connect();
