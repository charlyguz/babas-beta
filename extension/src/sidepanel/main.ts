import './panel.css';
import { PANEL_PORT_NAME } from '../shared/messages';
import { PanelConnection, type PortLike } from './connection';
import { t } from './i18n';
import { mountPanel, type ViewDeps } from './view';
import { VoiceRecorder, browserRecorderDeps, hasMicrophoneSupport } from './voice';

function voiceDeps(): ViewDeps['voice'] {
  if (!hasMicrophoneSupport(navigator)) return undefined;
  return { createRecorder: (hooks) => new VoiceRecorder(browserRecorderDeps(), hooks) };
}

function openMicPermission(): void {
  chrome.tabs.create({ url: chrome.runtime.getURL('micpermission.html') }).catch(() => undefined);
}

function connectPort(): PortLike {
  const port = chrome.runtime.connect({ name: PANEL_PORT_NAME });
  port.onDisconnect.addListener(() => {
    void chrome.runtime.lastError;
  });
  return port;
}

function start(): void {
  const root = document.getElementById('app');
  if (!root) return;
  try {
    document.documentElement.lang = chrome.i18n.getUILanguage().split('-')[0] || 'es';
  } catch {
    // the static lang attribute stays
  }
  let windowId: number | undefined;
  const connection: PanelConnection = new PanelConnection({
    connect: connectPort,
    onMessage: (message) => view.dispatch({ type: 'sw', message }),
    onLink: (up) => view.dispatch({ type: 'port', up }),
  });
  const view = mountPanel(root, {
    t,
    send: (message) => connection.send(message),
    getWindowId: () => windowId,
    voice: voiceDeps(),
    openMicPermission,
  });
  connection.start();
  chrome.windows
    .getCurrent()
    .then((current) => {
      windowId = current.id;
    })
    .catch(() => undefined);
}

start();
