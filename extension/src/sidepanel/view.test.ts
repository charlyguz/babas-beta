import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PanelToSw, SwToPanel } from '../shared/messages';
import { MAX_LOG_ENTRIES } from './model';
import { tEs } from './test-helpers';
import { mountPanel, type PanelView } from './view';

interface Harness {
  view: PanelView;
  sent: PanelToSw[];
  root: HTMLElement;
}

function setup(options: { armDelayMs?: number; windowId?: number; sendOk?: boolean } = {}): Harness {
  document.body.innerHTML = '<div id="app"></div>';
  const root = document.getElementById('app') as HTMLElement;
  const sent: PanelToSw[] = [];
  const view = mountPanel(root, {
    t: tEs,
    send: (message) => {
      if (options.sendOk === false) return false;
      sent.push(message);
      return true;
    },
    getWindowId: () => options.windowId,
    now: () => 1000,
    armDelayMs: options.armDelayMs ?? 0,
  });
  return { view, sent, root };
}

const sw = (h: Harness, message: SwToPanel): void => h.view.dispatch({ type: 'sw', message });
const online = (h: Harness): void => {
  h.view.dispatch({ type: 'port', up: true });
  sw(h, { type: 'status', connected: true, halted: false, running: false });
};
const q = <T extends HTMLElement = HTMLElement>(h: Harness, testid: string): T => {
  const node = h.root.querySelector<T>(`[data-testid="${testid}"]`);
  if (!node) throw new Error(`Missing [data-testid=${testid}]`);
  return node;
};
const qa = (h: Harness, testid: string): HTMLElement[] => [...h.root.querySelectorAll<HTMLElement>(`[data-testid="${testid}"]`)];
const key = (target: EventTarget, name: string, init: KeyboardEventInit = {}): KeyboardEvent => {
  const event = new KeyboardEvent('keydown', { key: name, bubbles: true, cancelable: true, ...init });
  target.dispatchEvent(event);
  return event;
};
const text = (node: HTMLElement): string => (node.textContent ?? '').replace(/\s+/g, ' ').trim();
const showConfirm = (h: Harness, id = 'c1'): void =>
  sw(h, {
    type: 'confirm',
    request_id: id,
    title: 'Escribir datos personales',
    lines: ['Escribir «ana@example.com» en «Correo» (localhost)', 'Presionar «Enviar» en localhost', 'Presionar la tecla Enter en «Correo» (localhost)'],
    explanation: 'Voy a llenar el formulario con su correo.',
  });

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = '';
});

describe('layout and strings', () => {
  it('renders every test id the end-to-end tests rely on, with Spanish strings', () => {
    const h = setup();
    for (const id of ['status', 'log', 'task-input', 'send', 'stop', 'resume', 'run-state', 'settings', 'settings-port', 'settings-sites']) {
      expect(q(h, id)).toBeTruthy();
    }
    expect(text(q(h, 'send'))).toBe('Enviar');
    expect(text(q(h, 'stop'))).toBe('Detener');
    expect(text(q(h, 'resume'))).toBe('Reanudar');
    expect(q<HTMLTextAreaElement>(h, 'task-input').placeholder).toBe('¿Qué quiere hacer? Por ejemplo: busque videos de recetas en YouTube');
    expect(h.root.querySelector('summary')?.textContent).toBe('Ajustes');
    expect(document.title).toBe('BABAS');
  });

  it('falls back to the key when a string is missing instead of rendering nothing', () => {
    document.body.innerHTML = '<div id="app"></div>';
    mountPanel(document.getElementById('app') as HTMLElement, { t: (k) => k, send: () => true });
    expect(document.querySelector('[data-testid=send]')?.textContent).toBe('send');
  });
});

describe('status pill and run state', () => {
  it('walks disconnected, connected, working, halted', () => {
    const h = setup();
    expect(text(q(h, 'status'))).toBe('Sin conexión con BABAS');
    expect(q(h, 'help').hidden).toBe(false);
    online(h);
    expect(text(q(h, 'status'))).toBe('Conectado');
    expect(q(h, 'help').hidden).toBe(true);
    sw(h, { type: 'status', connected: true, halted: false, running: true });
    expect(text(q(h, 'status'))).toBe('Trabajando…');
    sw(h, { type: 'status', connected: true, halted: true, running: false });
    expect(text(q(h, 'status'))).toBe('Detenido');
    h.view.dispatch({ type: 'port', up: false });
    expect(text(q(h, 'status'))).toBe('Sin conexión con BABAS');
  });

  it('shows done, failed, stopped and the summary in the run-state element', () => {
    const h = setup();
    online(h);
    expect(text(q(h, 'run-state'))).toBe('');
    sw(h, { type: 'run_state', state: 'running' });
    expect(text(q(h, 'run-state'))).toBe('Trabajando…');
    sw(h, { type: 'run_state', state: 'done', summary: 'Encontré 3 videos.' });
    expect(text(q(h, 'run-state'))).toBe('Listo. Encontré 3 videos.');
    sw(h, { type: 'run_state', state: 'failed' });
    expect(text(q(h, 'run-state'))).toBe('No pude terminar la tarea.');
    sw(h, { type: 'run_state', state: 'stopped' });
    expect(text(q(h, 'run-state'))).toBe('Detenido. No se hizo nada más.');
  });
});

describe('sending a task', () => {
  it('is disabled while disconnected or running and enabled when idle', () => {
    const h = setup();
    expect(q<HTMLButtonElement>(h, 'send').disabled).toBe(true);
    online(h);
    expect(q<HTMLButtonElement>(h, 'send').disabled).toBe(false);
    sw(h, { type: 'run_state', state: 'running' });
    expect(q<HTMLButtonElement>(h, 'send').disabled).toBe(true);
    sw(h, { type: 'run_state', state: 'done' });
    expect(q<HTMLButtonElement>(h, 'send').disabled).toBe(false);
  });

  it('sends on Enter, not on Shift+Enter, and never an empty task', () => {
    const h = setup({ windowId: 7 });
    online(h);
    const input = q<HTMLTextAreaElement>(h, 'task-input');
    input.value = '   ';
    expect(key(input, 'Enter').defaultPrevented).toBe(true);
    expect(h.sent).toEqual([]);
    input.value = 'busque recetas';
    expect(key(input, 'Enter', { shiftKey: true }).defaultPrevented).toBe(false);
    expect(h.sent).toEqual([]);
    key(input, 'Enter');
    expect(h.sent).toEqual([{ type: 'task.start', text: 'busque recetas', windowId: 7 }]);
    expect(input.value).toBe('');
    const bubble = qa(h, 'log-entry').at(-1) as HTMLElement;
    expect(bubble.dataset.kind).toBe('user');
    expect(text(bubble)).toBe('busque recetas');
  });

  it('sends with the button, omits windowId when unknown and trims the text', () => {
    const h = setup();
    online(h);
    q<HTMLTextAreaElement>(h, 'task-input').value = '  hola  ';
    q(h, 'send').click();
    expect(h.sent).toEqual([{ type: 'task.start', text: 'hola' }]);
  });

  it('does not send while running or disconnected, and keeps the text when delivery fails', () => {
    const idle = setup();
    const input = q<HTMLTextAreaElement>(idle, 'task-input');
    input.value = 'hola';
    key(input, 'Enter');
    expect(idle.sent).toEqual([]);
    expect(input.value).toBe('hola');

    const running = setup();
    online(running);
    sw(running, { type: 'run_state', state: 'running' });
    q<HTMLTextAreaElement>(running, 'task-input').value = 'otra';
    key(q(running, 'task-input'), 'Enter');
    expect(running.sent).toEqual([]);

    const broken = setup({ sendOk: false });
    online(broken);
    q<HTMLTextAreaElement>(broken, 'task-input').value = 'hola';
    q(broken, 'send').click();
    expect(q<HTMLTextAreaElement>(broken, 'task-input').value).toBe('hola');
    expect(qa(broken, 'log-entry')).toHaveLength(0);
  });
});

describe('conversation log', () => {
  it('renders each kind and marks steps by outcome', () => {
    const h = setup();
    const log = (kind: 'user' | 'assistant' | 'step' | 'result' | 'error' | 'info', t: string, ok?: boolean): void =>
      sw(h, { type: 'log', entry: { kind, text: t, ts: 1, ...(ok === undefined ? {} : { ok }) } });
    log('assistant', 'Voy a buscar.');
    log('step', 'Abrir YouTube', true);
    log('step', 'Presionar «Buscar»', false);
    log('step', 'Esperar');
    log('result', 'Listo, aquí está.');
    log('error', 'No pude abrir la página.');
    log('info', 'Pensando…');
    const entries = qa(h, 'log-entry');
    expect(entries.map((e) => e.dataset.kind)).toEqual(['assistant', 'step', 'step', 'step', 'result', 'error', 'info']);
    expect(entries[1]?.classList.contains('is-ok')).toBe(true);
    expect(entries[2]?.classList.contains('is-fail')).toBe(true);
    expect(entries[3]?.classList.contains('is-neutral')).toBe(true);
    expect(entries.map(text)).toContain('Abrir YouTube');
  });

  it('is an aria-live log region and scrolls to the newest entry', () => {
    const h = setup();
    const log = q(h, 'log');
    expect(log.getAttribute('role')).toBe('log');
    expect(log.getAttribute('aria-live')).toBe('polite');
    Object.defineProperty(log, 'scrollHeight', { value: 900, configurable: true });
    sw(h, { type: 'log', entry: { kind: 'info', text: 'uno', ts: 1 } });
    expect(log.scrollTop).toBe(900);
  });

  it('keeps the DOM in step with the capped log', () => {
    const h = setup();
    for (let i = 0; i < MAX_LOG_ENTRIES + 10; i += 1) sw(h, { type: 'log', entry: { kind: 'info', text: `n${i}`, ts: i } });
    const entries = qa(h, 'log-entry');
    expect(entries).toHaveLength(MAX_LOG_ENTRIES);
    expect(text(entries[0] as HTMLElement)).toBe('n10');
    expect(text(entries.at(-1) as HTMLElement)).toBe(`n${MAX_LOG_ENTRIES + 9}`);
  });
});

describe('confirmation card', () => {
  it('shows the title, the lines as a list, the explanation and both buttons', () => {
    const h = setup();
    online(h);
    showConfirm(h);
    const card = q(h, 'confirm-card');
    expect(text(q(h, 'confirm-title'))).toBe('Escribir datos personales');
    expect(qa(h, 'confirm-line').map(text)).toEqual([
      'Escribir «ana@example.com» en «Correo» (localhost)',
      'Presionar «Enviar» en localhost',
      'Presionar la tecla Enter en «Correo» (localhost)',
    ]);
    expect(card.querySelectorAll('ul > li')).toHaveLength(3);
    expect(text(q(h, 'confirm-explanation'))).toBe('BABAS dice: Voy a llenar el formulario con su correo.');
    expect(text(q(h, 'confirm-yes'))).toBe('Sí, continuar');
    expect(text(q(h, 'confirm-no'))).toBe('No');
  });

  it('omits the explanation line when there is no explanation', () => {
    const h = setup();
    sw(h, { type: 'confirm', request_id: 'c', title: 'T', lines: ['x'], explanation: '' });
    expect(h.root.querySelector('[data-testid=confirm-explanation]')).toBeNull();
  });

  it('focuses the card itself, never the approve button', () => {
    const h = setup();
    online(h);
    showConfirm(h);
    const card = q(h, 'confirm-card');
    expect(card.getAttribute('tabindex')).toBe('-1');
    expect(document.activeElement).toBe(card);
    expect(document.activeElement).not.toBe(q(h, 'confirm-yes'));
  });

  it('no key press answers a card', () => {
    const h = setup();
    online(h);
    showConfirm(h);
    for (const target of [q(h, 'confirm-card'), q(h, 'task-input'), document.body, document]) {
      for (const name of ['Enter', ' ', 'y', 'Escape']) key(target, name);
    }
    expect(h.sent).toEqual([]);
    expect(h.root.querySelector('[data-testid=confirm-card]')).not.toBeNull();
  });

  it('Sí sends an approval and removes the card; No sends a refusal', () => {
    const h = setup();
    online(h);
    showConfirm(h, 'c1');
    q(h, 'confirm-yes').click();
    expect(h.sent).toEqual([{ type: 'confirm.answer', request_id: 'c1', approved: true }]);
    expect(h.root.querySelector('[data-testid=confirm-card]')).toBeNull();
    showConfirm(h, 'c2');
    q(h, 'confirm-no').click();
    expect(h.sent.at(-1)).toEqual({ type: 'confirm.answer', request_id: 'c2', approved: false });
    expect(h.root.querySelector('[data-testid=confirm-card]')).toBeNull();
  });

  it('keeps the card when the answer cannot be delivered', () => {
    const h = setup({ sendOk: false });
    showConfirm(h);
    q(h, 'confirm-yes').click();
    expect(h.root.querySelector('[data-testid=confirm-card]')).not.toBeNull();
  });

  it('arms the approve button only after a short delay, so a double click cannot approve the next card', () => {
    const h = setup({ armDelayMs: 500 });
    online(h);
    showConfirm(h, 'c1');
    showConfirm(h, 'c2');
    const yes = q<HTMLButtonElement>(h, 'confirm-yes');
    expect(yes.disabled).toBe(true);
    vi.advanceTimersByTime(500);
    expect(yes.disabled).toBe(false);
    yes.click();
    const next = q<HTMLButtonElement>(h, 'confirm-yes');
    expect(next.disabled).toBe(true);
    next.click();
    expect(h.sent).toEqual([{ type: 'confirm.answer', request_id: 'c1', approved: true }]);
    expect(q(h, 'confirm-no').hasAttribute('disabled')).toBe(false);
  });

  it('queues further requests and shows them in arrival order', () => {
    const h = setup();
    online(h);
    showConfirm(h, 'a');
    sw(h, { type: 'allow', request_id: 'b', host: 'example.com', url: 'https://example.com/' });
    sw(h, { type: 'ask', request_id: 'c', question: '¿Cuál?', kind: 'text' });
    expect(qa(h, 'confirm-card')).toHaveLength(1);
    expect(h.root.querySelectorAll('[data-testid$="-card"]')).toHaveLength(1);
    q(h, 'confirm-no').click();
    expect(h.root.querySelector('[data-testid=allow-card]')).not.toBeNull();
    q(h, 'allow-no').click();
    expect(h.root.querySelector('[data-testid=ask-card]')).not.toBeNull();
  });

  it('request.closed removes the active card or a queued one without sending anything', () => {
    const h = setup();
    online(h);
    showConfirm(h, 'a');
    showConfirm(h, 'b');
    sw(h, { type: 'request.closed', request_id: 'b' });
    expect(qa(h, 'confirm-card')).toHaveLength(1);
    sw(h, { type: 'request.closed', request_id: 'a' });
    expect(h.root.querySelector('[data-testid=confirm-card]')).toBeNull();
    expect(h.sent).toEqual([]);
  });

  it('returns focus to the task box when the focused card goes away', () => {
    const h = setup();
    online(h);
    showConfirm(h);
    sw(h, { type: 'request.closed', request_id: 'c1' });
    expect(document.activeElement).toBe(q(h, 'task-input'));
  });
});

describe('ask cards', () => {
  it('text ask: reply is enabled once there is text, sends the answer, cancel sends null', () => {
    const h = setup();
    online(h);
    sw(h, { type: 'ask', request_id: 'k1', question: '¿Cuál es su nombre?', kind: 'text' });
    expect(text(q(h, 'ask-question'))).toBe('¿Cuál es su nombre?');
    const input = q<HTMLInputElement>(h, 'ask-input');
    expect(input.placeholder).toBe('Escriba su respuesta aquí');
    const reply = q<HTMLButtonElement>(h, 'ask-reply');
    expect(text(reply)).toBe('Responder');
    expect(reply.disabled).toBe(true);
    input.value = ' Ana ';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    expect(reply.disabled).toBe(false);
    reply.click();
    expect(h.sent).toEqual([{ type: 'ask.answer', request_id: 'k1', answer: 'Ana' }]);
    expect(h.root.querySelector('[data-testid=ask-card]')).toBeNull();

    sw(h, { type: 'ask', request_id: 'k2', question: '¿Otra?', kind: 'text' });
    expect(text(q(h, 'ask-cancel'))).toBe('Cancelar');
    q(h, 'ask-cancel').click();
    expect(h.sent.at(-1)).toEqual({ type: 'ask.answer', request_id: 'k2', answer: null });
  });

  it('Enter in the reply box sends the typed answer', () => {
    const h = setup();
    sw(h, { type: 'ask', request_id: 'k1', question: 'Q', kind: 'text' });
    const input = q<HTMLInputElement>(h, 'ask-input');
    key(input, 'Enter');
    expect(h.sent).toEqual([]);
    input.value = 'sí';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    key(input, 'Enter');
    expect(h.sent).toEqual([{ type: 'ask.answer', request_id: 'k1', answer: 'sí' }]);
  });

  it('handoff ask: shows the message and a single Listo button that answers with an empty string', () => {
    const h = setup();
    sw(h, { type: 'ask', request_id: 'h1', question: 'Resuelva el captcha y presione Listo.', kind: 'handoff' });
    const card = q(h, 'ask-card');
    expect(text(q(h, 'ask-question'))).toBe('Resuelva el captcha y presione Listo.');
    expect(card.querySelector('[data-testid=ask-input]')).toBeNull();
    expect(card.querySelectorAll('button')).toHaveLength(1);
    expect(text(q(h, 'handoff-done'))).toBe('Listo');
    q(h, 'handoff-done').click();
    expect(h.sent).toEqual([{ type: 'ask.answer', request_id: 'h1', answer: '' }]);
  });
});

describe('allow cards', () => {
  it('names the host and answers yes or no', () => {
    const h = setup();
    sw(h, { type: 'allow', request_id: 'a1', host: 'gob.mx', url: 'https://www.gob.mx/curp' });
    expect(text(q(h, 'allow-card'))).toContain('¿Permitir este sitio?');
    expect(text(q(h, 'allow-text'))).toBe('BABAS quiere usar el sitio gob.mx.');
    expect(text(q(h, 'allow-yes'))).toBe('Permitir');
    expect(text(q(h, 'allow-no'))).toBe('No permitir');
    q(h, 'allow-yes').click();
    expect(h.sent).toEqual([{ type: 'allow.answer', request_id: 'a1', approved: true }]);
    sw(h, { type: 'allow', request_id: 'a2', host: 'x.mx', url: 'https://x.mx/' });
    q(h, 'allow-no').click();
    expect(h.sent.at(-1)).toEqual({ type: 'allow.answer', request_id: 'a2', approved: false });
  });
});

describe('stop and resume', () => {
  it('stop is hidden when idle, visible while running or while a request is pending', () => {
    const h = setup();
    online(h);
    expect(q(h, 'stop').hidden).toBe(true);
    sw(h, { type: 'run_state', state: 'running' });
    expect(q(h, 'stop').hidden).toBe(false);
    sw(h, { type: 'run_state', state: 'done' });
    expect(q(h, 'stop').hidden).toBe(true);
    showConfirm(h);
    expect(q(h, 'stop').hidden).toBe(false);
  });

  it('stop sends the message at once, closes cards, and offers resume', () => {
    const h = setup();
    online(h);
    sw(h, { type: 'run_state', state: 'running' });
    showConfirm(h);
    q(h, 'stop').click();
    expect(h.sent).toEqual([{ type: 'stop' }]);
    expect(h.root.querySelector('[data-testid=confirm-card]')).toBeNull();
    expect(q(h, 'stop').hidden).toBe(true);
    expect(q(h, 'resume').hidden).toBe(false);
    expect(text(q(h, 'run-state'))).toBe('Detenido. No se hizo nada más.');
    expect(text(q(h, 'status'))).toBe('Detenido');
  });

  it('resume sends the message and hides once the worker reports no halt', () => {
    const h = setup();
    online(h);
    sw(h, { type: 'status', connected: true, halted: true, running: false });
    expect(q(h, 'resume').hidden).toBe(false);
    q(h, 'resume').click();
    expect(h.sent).toEqual([{ type: 'resume' }]);
    sw(h, { type: 'status', connected: true, halted: false, running: false });
    expect(q(h, 'resume').hidden).toBe(true);
    expect(text(q(h, 'run-state'))).toBe('');
    expect(text(q(h, 'status'))).toBe('Conectado');
  });
});

describe('settings', () => {
  it('lists the allowed sites with remove buttons and sends removeSite', () => {
    const h = setup();
    sw(h, { type: 'settings', bridgePort: 8765, sites: ['youtube.com', 'gob.mx'] });
    expect(q<HTMLInputElement>(h, 'settings-port').value).toBe('8765');
    expect(qa(h, 'site').map((n) => n.querySelector('.site-host')?.textContent)).toEqual(['youtube.com', 'gob.mx']);
    const buttons = qa(h, 'site-remove');
    expect(buttons.map(text)).toEqual(['Quitar', 'Quitar']);
    expect(buttons[1]?.getAttribute('aria-label')).toBe('Quitar gob.mx');
    buttons[1]?.click();
    expect(h.sent).toEqual([{ type: 'settings.set', removeSite: 'gob.mx' }]);
    expect(qa(h, 'site')).toHaveLength(2);
  });

  it('says when no site is allowed yet', () => {
    const h = setup();
    sw(h, { type: 'settings', bridgePort: 8765, sites: [] });
    expect(text(q(h, 'settings-no-sites'))).toBe('Ninguno todavía');
  });

  it('validates the port before sending it', () => {
    const h = setup();
    const port = q<HTMLInputElement>(h, 'settings-port');
    const change = (value: string): void => {
      port.value = value;
      port.dispatchEvent(new Event('change', { bubbles: true }));
    };
    for (const bad of ['0', '65536', '-1', '80.5', 'abc', '', '99999999']) {
      change(bad);
      expect(port.getAttribute('aria-invalid')).toBe('true');
    }
    expect(h.sent).toEqual([]);
    change('9000');
    expect(port.hasAttribute('aria-invalid')).toBe(false);
    change('65535');
    expect(h.sent).toEqual([
      { type: 'settings.set', bridgePort: 9000 },
      { type: 'settings.set', bridgePort: 65535 },
    ]);
  });
});

describe('untrusted text is rendered inert', () => {
  const evil = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=1</script><b>negrita</b>&amp;';

  it('renders markup from the model, the page or the user as plain text everywhere', () => {
    const h = setup();
    online(h);
    sw(h, { type: 'settings', bridgePort: 8765, sites: [evil] });
    for (const kind of ['user', 'assistant', 'step', 'result', 'error', 'info'] as const) {
      sw(h, { type: 'log', entry: { kind, text: evil, ok: true, ts: 1 } });
    }
    sw(h, { type: 'run_state', state: 'done', summary: evil });
    sw(h, { type: 'confirm', request_id: 'c', title: evil, lines: [evil, evil], explanation: evil });
    sw(h, { type: 'ask', request_id: 'k', question: evil, kind: 'text' });
    sw(h, { type: 'allow', request_id: 'a', host: evil, url: evil });

    const everything = [h.root, ...[...h.root.querySelectorAll('*')]];
    expect(h.root.querySelectorAll('img[src="x"], script, b').length).toBe(0);
    expect(everything.some((n) => n instanceof HTMLElement && n.hasAttribute('onerror'))).toBe(false);
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();

    for (const entry of qa(h, 'log-entry')) expect(entry.textContent).toBe(evil);
    expect(q(h, 'run-summary').textContent).toBe(evil);
    expect(q(h, 'confirm-title').textContent).toBe(evil);
    expect(qa(h, 'confirm-line').map((n) => n.textContent)).toEqual([evil, evil]);
    expect(q(h, 'confirm-explanation').textContent).toBe(`BABAS dice: ${evil}`);
    expect(q(h, 'site').querySelector('.site-host')?.textContent).toBe(evil);

    q(h, 'confirm-no').click();
    expect(q(h, 'ask-question').textContent).toBe(evil);
    q(h, 'ask-cancel').click();
    expect(q(h, 'allow-text').textContent).toBe(`BABAS quiere usar el sitio ${evil}.`);
    expect(h.root.querySelectorAll('img[src="x"], script, b').length).toBe(0);
  });
});
