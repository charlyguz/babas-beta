import type { PanelToSw } from '../shared/messages';
import type { Translate } from './i18n';
import {
  activeRequest,
  canSend,
  initialState,
  isDisconnected,
  reduce,
  runStateKey,
  showResume,
  showStop,
  statusKey,
  type LogItem,
  type PanelAction,
  type PanelState,
  type RequestItem,
} from './model';

export interface ViewDeps {
  t: Translate;
  send: (message: PanelToSw) => boolean;
  getWindowId?: () => number | undefined;
  now?: () => number;
  armDelayMs?: number;
}

export interface PanelView {
  dispatch(action: PanelAction): void;
  getState(): PanelState;
}

const DEFAULT_ARM_DELAY_MS = 500;
const MAX_TASK_LENGTH = 4000;

interface ElOptions {
  cls?: string;
  testid?: string;
  text?: string;
  attrs?: Record<string, string>;
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, options: ElOptions = {}, ...children: Node[]): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (options.cls) node.className = options.cls;
  if (options.testid) node.dataset.testid = options.testid;
  if (options.text !== undefined) node.textContent = options.text;
  for (const [name, value] of Object.entries(options.attrs ?? {})) node.setAttribute(name, value);
  for (const child of children) node.append(child);
  return node;
}

function validPort(raw: string): number | null {
  if (!/^\d{1,5}$/.test(raw.trim())) return null;
  const value = Number(raw.trim());
  return value >= 1 && value <= 65535 ? value : null;
}

let cardCounter = 0;

export function mountPanel(root: HTMLElement, deps: ViewDeps): PanelView {
  const { t } = deps;
  const now = deps.now ?? Date.now;
  const armDelayMs = deps.armDelayMs ?? DEFAULT_ARM_DELAY_MS;
  let state = initialState();
  let renderedRequestId: string | null = null;
  let cardNode: HTMLElement | null = null;

  root.replaceChildren();
  document.title = t('extName');

  const icon = el('img', { cls: 'brand-icon', attrs: { src: 'icons/icon32.png', alt: '', width: '32', height: '32', 'aria-hidden': 'true' } });
  const status = el('div', { cls: 'status', testid: 'status', attrs: { role: 'status' } });
  const header = el('header', { cls: 'header' }, icon, el('h1', { cls: 'brand', text: t('extName') }), status);
  const help = el('p', { cls: 'help', testid: 'help', text: t('notConnectedHelp') });

  const log = el('div', { cls: 'log', testid: 'log', attrs: { role: 'log', 'aria-live': 'polite', 'aria-relevant': 'additions' } });
  const requests = el('div', { cls: 'requests', testid: 'requests' });

  const runState = el('div', { cls: 'run-state', testid: 'run-state', attrs: { role: 'status', 'aria-live': 'polite' } });
  const stop = el('button', { cls: 'btn btn-danger btn-block', testid: 'stop', text: t('stop'), attrs: { type: 'button' } });
  const resume = el('button', { cls: 'btn btn-block', testid: 'resume', text: t('resume'), attrs: { type: 'button' } });
  const input = el('textarea', {
    cls: 'task-input',
    testid: 'task-input',
    attrs: { rows: '3', maxlength: String(MAX_TASK_LENGTH), placeholder: t('inputPlaceholder'), 'aria-label': t('inputPlaceholder'), enterkeyhint: 'send' },
  });
  const send = el('button', { cls: 'btn btn-primary send', testid: 'send', text: t('send'), attrs: { type: 'button' } });
  const compose = el('div', { cls: 'compose' }, input, send);
  const footer = el('footer', { cls: 'footer' }, runState, stop, resume, compose);

  const portInput = el('input', {
    cls: 'port-input',
    testid: 'settings-port',
    attrs: { type: 'number', min: '1', max: '65535', step: '1', inputmode: 'numeric', id: 'babas-port' },
  });
  const sitesList = el('ul', { cls: 'sites', testid: 'settings-sites' });
  const settings = el(
    'details',
    { cls: 'settings', testid: 'settings' },
    el('summary', { text: t('settings') }),
    el('div', { cls: 'settings-body' }, el('label', { cls: 'field-label', text: t('settingsPort'), attrs: { for: 'babas-port' } }), portInput, el('h2', { cls: 'field-label', text: t('settingsSites') }), sitesList),
  );

  root.append(header, help, log, requests, footer, settings);

  const dispatch = (action: PanelAction): void => {
    state = reduce(state, action);
    render();
  };

  const submit = (): void => {
    const text = input.value.trim();
    if (!text || !canSend(state)) return;
    const windowId = deps.getWindowId?.();
    const message: PanelToSw = windowId === undefined ? { type: 'task.start', text } : { type: 'task.start', text, windowId };
    if (!deps.send(message)) return;
    input.value = '';
    dispatch({ type: 'user.sent', text, ts: now() });
  };

  send.addEventListener('click', submit);
  input.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    submit();
  });
  stop.addEventListener('click', () => {
    if (deps.send({ type: 'stop' })) dispatch({ type: 'user.stopped' });
  });
  resume.addEventListener('click', () => {
    deps.send({ type: 'resume' });
  });

  portInput.addEventListener('input', () => {
    if (validPort(portInput.value) !== null) portInput.removeAttribute('aria-invalid');
  });
  portInput.addEventListener('change', () => {
    const port = validPort(portInput.value);
    if (port === null) {
      portInput.setAttribute('aria-invalid', 'true');
      return;
    }
    portInput.removeAttribute('aria-invalid');
    deps.send({ type: 'settings.set', bridgePort: port });
  });

  function answer(item: RequestItem, message: PanelToSw): void {
    if (deps.send(message)) dispatch({ type: 'request.answered', request_id: item.id });
  }

  function arm(button: HTMLButtonElement): void {
    if (armDelayMs <= 0) return;
    button.disabled = true;
    setTimeout(() => {
      button.disabled = false;
    }, armDelayMs);
  }

  function button(label: string, testid: string, cls: string, onClick: () => void): HTMLButtonElement {
    const node = el('button', { cls: `btn ${cls}`.trim(), testid, text: label, attrs: { type: 'button' } });
    node.addEventListener('click', onClick);
    return node;
  }

  function buildCard(item: RequestItem): HTMLElement {
    const id = `card-${(cardCounter += 1)}`;
    const common = { tabindex: '-1' };
    switch (item.kind) {
      case 'confirm': {
        const lines = el('ul', { cls: 'lines', testid: 'confirm-lines', attrs: { id: `${id}-lines` } });
        for (const line of item.lines) lines.append(el('li', { text: line, testid: 'confirm-line' }));
        const yes = button(t('confirmYes'), 'confirm-yes', 'btn-primary', () => answer(item, { type: 'confirm.answer', request_id: item.id, approved: true }));
        const no = button(t('confirmNo'), 'confirm-no', '', () => answer(item, { type: 'confirm.answer', request_id: item.id, approved: false }));
        arm(yes);
        const card = el(
          'section',
          { cls: 'card card-confirm', testid: 'confirm-card', attrs: { ...common, role: 'alertdialog', 'aria-labelledby': `${id}-title`, 'aria-describedby': `${id}-lines` } },
          el('h2', { cls: 'card-title', testid: 'confirm-title', text: item.title, attrs: { id: `${id}-title` } }),
          lines,
        );
        if (item.explanation) {
          card.append(el('p', { cls: 'says', testid: 'confirm-explanation' }, el('strong', { text: t('babasSays') }), document.createTextNode(' '), el('span', { text: item.explanation })));
        }
        card.append(el('div', { cls: 'actions' }, yes, no));
        return card;
      }
      case 'allow': {
        const yes = button(t('allowYes'), 'allow-yes', 'btn-primary', () => answer(item, { type: 'allow.answer', request_id: item.id, approved: true }));
        const no = button(t('allowNo'), 'allow-no', '', () => answer(item, { type: 'allow.answer', request_id: item.id, approved: false }));
        arm(yes);
        return el(
          'section',
          { cls: 'card card-allow', testid: 'allow-card', attrs: { ...common, role: 'alertdialog', 'aria-labelledby': `${id}-title`, 'aria-describedby': `${id}-text` } },
          el('h2', { cls: 'card-title', text: t('allowTitle'), attrs: { id: `${id}-title` } }),
          el('p', { cls: 'card-text', testid: 'allow-text', text: t('allowText', [item.host]), attrs: { id: `${id}-text` } }),
          el('div', { cls: 'actions' }, yes, no),
        );
      }
      case 'ask': {
        const question = el('p', { cls: 'card-text', testid: 'ask-question', text: item.question, attrs: { id: `${id}-question` } });
        const card = el('section', { cls: 'card card-ask', testid: 'ask-card', attrs: { ...common, role: 'dialog', 'aria-labelledby': `${id}-question` } }, question);
        if (item.askKind === 'handoff') {
          const done = button(t('handoffDone'), 'handoff-done', 'btn-primary', () => answer(item, { type: 'ask.answer', request_id: item.id, answer: '' }));
          arm(done);
          card.append(el('div', { cls: 'actions' }, done));
          return card;
        }
        const field = el('input', {
          cls: 'ask-input',
          testid: 'ask-input',
          attrs: { type: 'text', maxlength: String(MAX_TASK_LENGTH), placeholder: t('askPlaceholder'), 'aria-labelledby': `${id}-question`, autocomplete: 'off' },
        });
        const reply = button(t('askReply'), 'ask-reply', 'btn-primary', () => {
          const text = field.value.trim();
          if (text) answer(item, { type: 'ask.answer', request_id: item.id, answer: text });
        });
        const cancel = button(t('askCancel'), 'ask-cancel', '', () => answer(item, { type: 'ask.answer', request_id: item.id, answer: null }));
        reply.disabled = true;
        field.addEventListener('input', () => {
          reply.disabled = field.value.trim() === '';
        });
        field.addEventListener('keydown', (event) => {
          if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
          event.preventDefault();
          reply.click();
        });
        card.append(field, el('div', { cls: 'actions' }, reply, cancel));
        return card;
      }
    }
  }

  function renderRequests(): void {
    const item = activeRequest(state);
    const nextId = item ? item.id : null;
    if (nextId === renderedRequestId) return;
    const hadFocus = cardNode !== null && cardNode.contains(document.activeElement);
    renderedRequestId = nextId;
    cardNode = null;
    requests.replaceChildren();
    if (!item) {
      if (hadFocus) input.focus();
      return;
    }
    cardNode = buildCard(item);
    requests.append(cardNode);
    cardNode.focus();
  }

  function renderLogEntry(entry: LogItem): HTMLElement {
    const classes = ['entry', `entry-${entry.kind}`];
    if (entry.kind === 'step') classes.push(entry.ok === true ? 'is-ok' : entry.ok === false ? 'is-fail' : 'is-neutral');
    const node = el('div', { cls: classes.join(' '), testid: 'log-entry', attrs: { 'data-kind': entry.kind, 'data-log-id': String(entry.id) } });
    node.append(el('span', { cls: 'entry-text', text: entry.text }));
    return node;
  }

  function renderLog(): void {
    const first = state.log[0];
    while (log.firstElementChild && first && Number((log.firstElementChild as HTMLElement).dataset.logId) < first.id) log.firstElementChild.remove();
    const last = log.lastElementChild as HTMLElement | null;
    const lastId = last ? Number(last.dataset.logId) : 0;
    let appended = false;
    for (const entry of state.log) {
      if (entry.id <= lastId) continue;
      log.append(renderLogEntry(entry));
      appended = true;
    }
    if (appended) log.scrollTop = log.scrollHeight;
  }

  function renderStatus(): void {
    const key = statusKey(state);
    status.textContent = t(key);
    status.dataset.state = key;
    help.hidden = !isDisconnected(state);
  }

  function renderRunState(): void {
    const key = runStateKey(state);
    if (!key) {
      runState.replaceChildren();
      return;
    }
    const parts: Node[] = [el('strong', { cls: 'run-label', text: t(key) })];
    if (state.summary) parts.push(document.createTextNode(' '), el('span', { cls: 'run-summary', testid: 'run-summary', text: state.summary }));
    runState.replaceChildren(...parts);
  }

  function renderSettings(): void {
    if (document.activeElement !== portInput) {
      portInput.value = state.bridgePort === null ? '' : String(state.bridgePort);
      portInput.removeAttribute('aria-invalid');
    }
    const hadFocus = sitesList.contains(document.activeElement);
    const items: Node[] = [];
    if (state.sites.length === 0) {
      items.push(el('li', { cls: 'no-sites', testid: 'settings-no-sites', text: t('settingsNoSites') }));
    }
    for (const site of state.sites) {
      const remove = el('button', {
        cls: 'btn btn-small',
        testid: 'site-remove',
        text: t('remove'),
        attrs: { type: 'button', 'aria-label': `${t('remove')} ${site}` },
      });
      remove.addEventListener('click', () => {
        deps.send({ type: 'settings.set', removeSite: site });
      });
      items.push(el('li', { cls: 'site', testid: 'site' }, el('span', { cls: 'site-host', text: site }), remove));
    }
    sitesList.replaceChildren(...items);
    if (hadFocus) {
      const next = sitesList.querySelector<HTMLElement>('button') ?? settings.querySelector<HTMLElement>('summary');
      next?.focus();
    }
  }

  let renderedSites = '';
  function render(): void {
    root.classList.toggle('has-request', state.queue.length > 0);
    renderStatus();
    renderLog();
    renderRequests();
    renderRunState();
    send.disabled = !canSend(state);
    stop.hidden = !showStop(state);
    resume.hidden = !showResume(state);
    const sitesKey = JSON.stringify([state.bridgePort, state.sites]);
    if (sitesKey !== renderedSites) {
      renderedSites = sitesKey;
      renderSettings();
    }
  }

  render();
  return { dispatch, getState: () => state };
}
