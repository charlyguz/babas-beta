import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PanelToSw, SwToPanel } from '../shared/messages';
import { MAX_LOG_ENTRIES } from './model';
import { tEs } from './test-helpers';
import { mountPanel, type PanelView } from './view';
import { VoiceCaptureError, type RecordedAudio, type RecorderHooks, type VoiceRecorderLike } from './voice';

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

class FakeRecorder implements VoiceRecorderLike {
  active = false;
  started = 0;
  finished = 0;
  cancelled = 0;
  finishError: unknown = null;
  private gate: { resolve: () => void; reject: (reason: unknown) => void } | null = null;

  constructor(readonly hooks: RecorderHooks) {}

  start(): Promise<void> {
    this.started += 1;
    this.active = true;
    return new Promise<void>((resolve, reject) => {
      this.gate = { resolve, reject };
    });
  }

  grant(): void {
    this.gate?.resolve();
    this.gate = null;
  }

  deny(error: unknown): void {
    this.active = false;
    this.gate?.reject(error);
    this.gate = null;
  }

  finish(): RecordedAudio {
    this.finished += 1;
    this.active = false;
    if (this.finishError) throw this.finishError;
    return { audioBase64: 'QUJD', durationMs: 1234 };
  }

  cancel(): void {
    this.cancelled += 1;
    this.active = false;
    this.gate?.reject(new VoiceCaptureError('cancelled', 'cancelled'));
    this.gate = null;
  }
}

interface VoiceHarness extends Harness {
  recorders: FakeRecorder[];
  opened: { count: number };
  rec(): FakeRecorder;
}

function setupVoice(options: { sendOk?: boolean; voice?: boolean; timeoutMs?: number } = {}): VoiceHarness {
  document.body.innerHTML = '<div id="app"></div>';
  const root = document.getElementById('app') as HTMLElement;
  const sent: PanelToSw[] = [];
  const recorders: FakeRecorder[] = [];
  const opened = { count: 0 };
  let ids = 0;
  const view = mountPanel(root, {
    t: tEs,
    send: (message) => {
      if (options.sendOk === false) return false;
      sent.push(message);
      return true;
    },
    now: () => 1000,
    armDelayMs: 0,
    voice:
      options.voice === false
        ? undefined
        : {
            createRecorder: (hooks) => {
              const recorder = new FakeRecorder(hooks);
              recorders.push(recorder);
              return recorder;
            },
          },
    openMicPermission: () => {
      opened.count += 1;
    },
    newRequestId: () => `voz-${(ids += 1)}`,
    transcribeTimeoutMs: options.timeoutMs ?? 100_000,
  });
  return { view, sent, root, recorders, opened, rec: () => recorders[recorders.length - 1] as FakeRecorder };
}

const flush = async (): Promise<void> => {
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
};
const mic = (h: Harness): HTMLButtonElement => q<HTMLButtonElement>(h, 'mic');
const input = (h: Harness): HTMLTextAreaElement => q<HTMLTextAreaElement>(h, 'task-input');
const voiceText = (h: Harness): string => text(q(h, 'voice-status'));

async function startRecording(h: VoiceHarness): Promise<FakeRecorder> {
  online(h);
  mic(h).click();
  const recorder = h.rec();
  recorder.grant();
  await flush();
  return recorder;
}

async function finishWithTranscript(h: VoiceHarness, transcript: string): Promise<void> {
  await startRecording(h);
  mic(h).click();
  const request = h.sent.at(-1);
  if (request?.type !== 'voice.audio') throw new Error('No audio was sent');
  sw(h, { type: 'voice.result', request_id: request.request_id, text: transcript });
}

describe('voice: the microphone button', () => {
  it('sits next to Enviar with a visible label, an inline icon and a test id', () => {
    const h = setupVoice();
    const button = mic(h);
    expect(button.dataset.testid).toBe('mic');
    expect(text(button)).toBe('Hablar');
    expect(button.parentElement).toBe(q(h, 'send').parentElement);
    const svg = button.querySelector('svg');
    expect(svg).not.toBeNull();
    expect(button.querySelectorAll('svg[aria-hidden="true"]').length).toBe(button.querySelectorAll('svg').length);
    expect(button.querySelector('img')).toBeNull();
    expect(/\p{Extended_Pictographic}/u.test(button.textContent ?? '')).toBe(false);
    expect(button.getAttribute('type')).toBe('button');
  });

  it('is hidden when the browser cannot record', () => {
    const h = setupVoice({ voice: false });
    expect(mic(h).hidden).toBe(true);
    expect(q(h, 'voice').hidden).toBe(true);
    online(h);
    expect(mic(h).hidden).toBe(true);
  });

  it('is shown when the browser can record', () => {
    const h = setupVoice();
    expect(mic(h).hidden).toBe(false);
  });

  it('is disabled while disconnected and enabled once connected', () => {
    const h = setupVoice();
    expect(mic(h).disabled).toBe(true);
    mic(h).click();
    expect(h.recorders).toHaveLength(0);
    online(h);
    expect(mic(h).disabled).toBe(false);
    h.view.dispatch({ type: 'port', up: false });
    expect(mic(h).disabled).toBe(true);
  });

  it('keeps an aria-live status region, empty at first', () => {
    const h = setupVoice();
    const region = q(h, 'voice-status');
    expect(region.getAttribute('aria-live')).toBe('polite');
    expect(region.getAttribute('role')).toBe('status');
    expect(voiceText(h)).toBe('');
    expect(q(h, 'voice-level').hidden).toBe(true);
    expect(q(h, 'voice-permission').hidden).toBe(true);
  });

  it('can be used while a task is running', async () => {
    const h = setupVoice();
    online(h);
    sw(h, { type: 'run_state', state: 'running' });
    expect(mic(h).disabled).toBe(false);
  });
});

describe('voice: recording', () => {
  it('asks for the microphone on click and shows the preparing state, disabled', () => {
    const h = setupVoice();
    online(h);
    mic(h).click();
    expect(h.recorders).toHaveLength(1);
    expect(h.rec().started).toBe(1);
    expect(h.view.getState().voice).toBe('starting');
    expect(voiceText(h)).toBe('Preparando el micrófono…');
    expect(mic(h).disabled).toBe(true);
    expect(text(mic(h))).toBe('Hablar');
  });

  it('shows the listening state, the live level and turns the button into Listo', async () => {
    const h = setupVoice();
    await startRecording(h);
    expect(h.view.getState().voice).toBe('recording');
    expect(text(mic(h))).toBe('Listo');
    expect(mic(h).dataset.phase).toBe('recording');
    expect(mic(h).disabled).toBe(false);
    expect(voiceText(h)).toContain('Estoy escuchando…');
    expect(voiceText(h)).toContain('presione «Listo»');
    const level = q(h, 'voice-level');
    expect(level.hidden).toBe(false);
    expect(level.getAttribute('aria-hidden')).toBe('true');
    h.rec().hooks.onLevel?.(0.456);
    expect(level.querySelector<HTMLElement>('.voice-level-bar')?.style.getPropertyValue('--level')).toBe('0.46');
  });

  it('does not touch the textarea or send a task while recording', async () => {
    const h = setupVoice();
    input(h).value = 'mi texto';
    await startRecording(h);
    expect(input(h).value).toBe('mi texto');
    expect(h.sent.filter((m) => m.type === 'task.start')).toEqual([]);
  });

  it('clicking Listo sends the audio once and shows the transcribing state', async () => {
    const h = setupVoice();
    const recorder = await startRecording(h);
    mic(h).click();
    expect(recorder.finished).toBe(1);
    expect(h.sent).toEqual([{ type: 'voice.audio', request_id: 'voz-1', audio_base64: 'QUJD', duration_ms: 1234 }]);
    expect(h.view.getState()).toMatchObject({ voice: 'transcribing', voiceRequestId: 'voz-1' });
    expect(voiceText(h)).toBe('Escribiendo lo que dijo…');
    expect(mic(h).disabled).toBe(true);
    mic(h).click();
    expect(h.sent).toHaveLength(1);
    expect(h.recorders).toHaveLength(1);
  });

  it('the automatic stop finishes the recording the same way', async () => {
    const h = setupVoice();
    const recorder = await startRecording(h);
    recorder.hooks.onAutoStop?.();
    expect(recorder.finished).toBe(1);
    expect(h.sent.map((m) => m.type)).toEqual(['voice.audio']);
    expect(h.view.getState().voice).toBe('transcribing');
  });

  it('ignores an automatic stop that arrives after the recording ended', async () => {
    const h = setupVoice();
    const recorder = await startRecording(h);
    mic(h).click();
    recorder.hooks.onAutoStop?.();
    expect(h.sent).toHaveLength(1);
  });

  it('uses a fresh request id for every recording', async () => {
    const h = setupVoice();
    await finishWithTranscript(h, 'uno');
    mic(h).click();
    h.rec().grant();
    await flush();
    mic(h).click();
    const ids = h.sent.flatMap((m) => (m.type === 'voice.audio' ? [m.request_id] : []));
    expect(ids).toEqual(['voz-1', 'voz-2']);
  });
});

describe('voice: the transcript fills the text box', () => {
  it('fills an empty box, focuses it with the caret at the end and sends nothing else', async () => {
    const h = setupVoice();
    await finishWithTranscript(h, 'busque videos de recetas');
    expect(input(h).value).toBe('busque videos de recetas');
    expect(document.activeElement).toBe(input(h));
    expect(input(h).selectionStart).toBe(input(h).value.length);
    expect(input(h).selectionEnd).toBe(input(h).value.length);
    expect(h.sent.map((m) => m.type)).toEqual(['voice.audio']);
    expect(h.view.getState().voice).toBe('idle');
    expect(mic(h).disabled).toBe(false);
    expect(text(mic(h))).toBe('Hablar');
    expect(voiceText(h)).toBe('');
  });

  it('adds a single space when the box already has text', async () => {
    const h = setupVoice();
    online(h);
    input(h).value = 'busque';
    await finishWithTranscript(h, 'recetas de pozole');
    expect(input(h).value).toBe('busque recetas de pozole');
  });

  it('does not double the space when the box already ends in whitespace', async () => {
    const h = setupVoice();
    online(h);
    input(h).value = 'busque ';
    await finishWithTranscript(h, 'recetas');
    expect(input(h).value).toBe('busque recetas');
    const second = setupVoice();
    online(second);
    input(second).value = 'busque\n';
    await finishWithTranscript(second, 'recetas');
    expect(input(second).value).toBe('busque\nrecetas');
  });

  it('collapses stray whitespace in the transcript', async () => {
    const h = setupVoice();
    await finishWithTranscript(h, '  busque   videos\n de  recetas  ');
    expect(input(h).value).toBe('busque videos de recetas');
  });

  it('keeps the box within its length limit', async () => {
    const h = setupVoice();
    online(h);
    input(h).value = 'a'.repeat(3990);
    await finishWithTranscript(h, 'b'.repeat(100));
    expect(input(h).value).toHaveLength(4000);
    expect(input(h).value.startsWith('a'.repeat(3990))).toBe(true);
  });

  it('puts markup in the box as plain text', async () => {
    const h = setupVoice();
    await finishWithTranscript(h, '<img src=x onerror="window.__pwned=1"><b>hola</b>');
    expect(input(h).value).toBe('<img src=x onerror="window.__pwned=1"><b>hola</b>');
    expect(h.root.querySelectorAll('img[src="x"], b').length).toBe(0);
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it('the user still has to press Enviar, and then the task goes through the normal path', async () => {
    const h = setupVoice();
    await finishWithTranscript(h, 'busque recetas');
    expect(h.sent.some((m) => m.type === 'task.start')).toBe(false);
    q(h, 'send').click();
    expect(h.sent.at(-1)).toEqual({ type: 'task.start', text: 'busque recetas' });
  });

  it('a blank transcript says nothing was heard and leaves the box alone', async () => {
    const h = setupVoice();
    online(h);
    input(h).value = 'hola';
    await finishWithTranscript(h, '   ');
    expect(input(h).value).toBe('hola');
    expect(text(q(h, 'voice-error'))).toBe(tEs('voiceErrEmpty'));
  });

  it('ignores a transcript for a request it does not know', async () => {
    const h = setupVoice();
    await startRecording(h);
    mic(h).click();
    sw(h, { type: 'voice.result', request_id: 'otro', text: 'intruso' });
    expect(input(h).value).toBe('');
    expect(h.view.getState().voice).toBe('transcribing');
  });

  it('ignores a transcript that arrives after a task was sent', async () => {
    const h = setupVoice();
    await startRecording(h);
    input(h).value = 'otra tarea';
    mic(h).click();
    q(h, 'send').click();
    sw(h, { type: 'voice.result', request_id: 'voz-1', text: 'tarde' });
    expect(input(h).value).toBe('');
    expect(h.view.getState().voice).toBe('idle');
  });
});

describe('voice: errors', () => {
  it.each([
    ['no_connection', 'voiceErrNoConnection'],
    ['timeout', 'voiceErrTimeout'],
    ['busy', 'voiceErrBusy'],
    ['not_configured', 'voiceErrNotConfigured'],
    ['provider', 'voiceErrProvider'],
    ['bad_audio', 'voiceErrBadAudio'],
    ['empty', 'voiceErrEmpty'],
  ] as const)('shows the readable text for %s, never the provider message', async (code, key) => {
    const h = setupVoice();
    await startRecording(h);
    mic(h).click();
    sw(h, { type: 'voice.error', request_id: 'voz-1', code, message: 'detalle técnico del proveedor' });
    const error = q(h, 'voice-error');
    expect(text(error)).toBe(tEs(key));
    expect(text(error)).not.toContain('proveedor');
    expect(tEs(key)).not.toBe(key);
    expect(error.closest('[aria-live="polite"]')).toBe(q(h, 'voice-status'));
    expect(h.view.getState().voice).toBe('idle');
    expect(mic(h).disabled).toBe(false);
    expect(input(h).value).toBe('');
  });

  it('clears the error when the next attempt starts', async () => {
    const h = setupVoice();
    await startRecording(h);
    mic(h).click();
    sw(h, { type: 'voice.error', request_id: 'voz-1', code: 'busy', message: 'x' });
    expect(h.root.querySelector('[data-testid=voice-error]')).not.toBeNull();
    mic(h).click();
    expect(h.root.querySelector('[data-testid=voice-error]')).toBeNull();
    expect(h.view.getState().voiceError).toBeNull();
    expect(voiceText(h)).toBe('Preparando el micrófono…');
  });

  it('a permission refusal explains itself and offers Dar permiso', async () => {
    const h = setupVoice();
    online(h);
    mic(h).click();
    h.rec().deny(Object.assign(new Error('refused'), { name: 'NotAllowedError' }));
    await flush();
    expect(h.view.getState()).toMatchObject({ voice: 'idle', voiceError: 'permission' });
    expect(text(q(h, 'voice-error'))).toBe(tEs('voiceErrPermission'));
    const grant = q<HTMLButtonElement>(h, 'voice-permission');
    expect(grant.hidden).toBe(false);
    expect(text(grant)).toBe('Dar permiso');
    expect(h.recorders).toHaveLength(1);
    grant.click();
    expect(h.opened.count).toBe(1);
  });

  it('hides Dar permiso again on the next attempt and for other errors', async () => {
    const h = setupVoice();
    online(h);
    mic(h).click();
    h.rec().deny(new VoiceCaptureError('permission', 'x'));
    await flush();
    mic(h).click();
    expect(q(h, 'voice-permission').hidden).toBe(true);
    h.rec().deny(new VoiceCaptureError('no_device', 'x'));
    await flush();
    expect(text(q(h, 'voice-error'))).toBe(tEs('voiceErrNoDevice'));
    expect(q(h, 'voice-permission').hidden).toBe(true);
  });

  it.each([
    [new VoiceCaptureError('mic_busy', 'x'), 'voiceErrMicBusy'],
    [new VoiceCaptureError('unsupported', 'x'), 'voiceErrCapture'],
    [new VoiceCaptureError('capture', 'x'), 'voiceErrCapture'],
    [new Error('surprise'), 'voiceErrCapture'],
  ])('maps the recorder failure %# to its message', async (error, key) => {
    const h = setupVoice();
    online(h);
    mic(h).click();
    h.rec().deny(error);
    await flush();
    expect(text(q(h, 'voice-error'))).toBe(tEs(key));
  });

  it('a recording that is too short reports that nothing was heard and frees the microphone', async () => {
    const h = setupVoice();
    const recorder = await startRecording(h);
    recorder.finishError = new VoiceCaptureError('empty', 'too short');
    mic(h).click();
    expect(recorder.cancelled).toBeGreaterThan(0);
    expect(text(q(h, 'voice-error'))).toBe(tEs('voiceErrEmpty'));
    expect(h.sent).toEqual([]);
    expect(h.view.getState().voice).toBe('idle');
  });

  it('reports no connection when the audio cannot be delivered', async () => {
    const h = setupVoice({ sendOk: false });
    online(h);
    mic(h).click();
    h.rec().grant();
    await flush();
    mic(h).click();
    expect(h.view.getState()).toMatchObject({ voice: 'idle', voiceError: 'no_connection' });
    expect(text(q(h, 'voice-error'))).toBe(tEs('voiceErrNoConnection'));
  });

  it('gives up with a timeout error when the worker never answers, and ignores the late answer', async () => {
    const h = setupVoice({ timeoutMs: 5000 });
    await startRecording(h);
    mic(h).click();
    vi.advanceTimersByTime(4999);
    expect(h.view.getState().voice).toBe('transcribing');
    vi.advanceTimersByTime(1);
    expect(h.view.getState()).toMatchObject({ voice: 'idle', voiceError: 'timeout' });
    expect(text(q(h, 'voice-error'))).toBe(tEs('voiceErrTimeout'));
    sw(h, { type: 'voice.result', request_id: 'voz-1', text: 'tarde' });
    expect(input(h).value).toBe('');
  });

  it('does not fire the timeout once the answer arrived', async () => {
    const h = setupVoice({ timeoutMs: 5000 });
    await finishWithTranscript(h, 'hola');
    vi.advanceTimersByTime(60_000);
    expect(h.view.getState().voiceError).toBeNull();
  });

  it('puts the focus back on the button after an error when it lost it', async () => {
    const h = setupVoice();
    online(h);
    mic(h).focus();
    mic(h).click();
    mic(h).blur();
    h.rec().deny(new VoiceCaptureError('no_device', 'x'));
    await flush();
    expect(document.activeElement).toBe(mic(h));
  });
});

describe('voice: the microphone is always released', () => {
  it('when the user clicks the button again', async () => {
    const h = setupVoice();
    const recorder = await startRecording(h);
    mic(h).click();
    expect(recorder.finished).toBe(1);
    expect(recorder.active).toBe(false);
  });

  it('when the link to the worker drops during the recording', async () => {
    const h = setupVoice();
    const recorder = await startRecording(h);
    h.view.dispatch({ type: 'port', up: false });
    expect(recorder.cancelled).toBe(1);
    expect(recorder.active).toBe(false);
    expect(h.view.getState()).toMatchObject({ voice: 'idle', voiceError: 'no_connection' });
    expect(text(q(h, 'voice-error'))).toBe(tEs('voiceErrNoConnection'));
    expect(h.sent).toEqual([]);
  });

  it('when the bridge drops during the recording', async () => {
    const h = setupVoice();
    const recorder = await startRecording(h);
    sw(h, { type: 'status', connected: false, halted: false, running: false });
    expect(recorder.cancelled).toBe(1);
    expect(h.view.getState().voice).toBe('idle');
  });

  it('when the link drops while the permission prompt is open, even if the stream arrives later', async () => {
    const h = setupVoice();
    online(h);
    mic(h).click();
    const recorder = h.rec();
    h.view.dispatch({ type: 'port', up: false });
    expect(recorder.cancelled).toBe(1);
    recorder.grant();
    await flush();
    expect(h.view.getState().voice).toBe('idle');
    expect(h.sent).toEqual([]);
  });

  it('when the panel unloads', async () => {
    const h = setupVoice();
    const recorder = await startRecording(h);
    window.dispatchEvent(new Event('pagehide'));
    expect(recorder.cancelled).toBe(1);
    expect(recorder.active).toBe(false);
  });

  it('when the panel unloads during the permission prompt', async () => {
    const h = setupVoice();
    online(h);
    mic(h).click();
    const recorder = h.rec();
    window.dispatchEvent(new Event('pagehide'));
    expect(recorder.cancelled).toBe(1);
    recorder.grant();
    await flush();
    expect(h.view.getState().voice).toBe('starting');
  });

  it('when the view is disposed, and the unload listener goes away with it', async () => {
    const h = setupVoice();
    const recorder = await startRecording(h);
    h.view.dispose();
    expect(recorder.cancelled).toBe(1);
    window.dispatchEvent(new Event('pagehide'));
    expect(recorder.cancelled).toBe(1);
  });

  it('when a task is sent in the middle of a recording', async () => {
    const h = setupVoice();
    const recorder = await startRecording(h);
    input(h).value = 'busque recetas';
    q(h, 'send').click();
    expect(recorder.cancelled).toBe(1);
    expect(h.view.getState().voice).toBe('idle');
    expect(h.sent.at(-1)).toEqual({ type: 'task.start', text: 'busque recetas' });
  });

  it('when starting fails, the recorder is not kept around', async () => {
    const h = setupVoice();
    online(h);
    mic(h).click();
    h.rec().deny(new VoiceCaptureError('permission', 'x'));
    await flush();
    mic(h).click();
    expect(h.recorders).toHaveLength(2);
    h.rec().grant();
    await flush();
    expect(h.view.getState().voice).toBe('recording');
  });
});

describe('voice: Stop is unrelated', () => {
  it('Stop keeps working while recording, and the recording goes on', async () => {
    const h = setupVoice();
    const recorder = await startRecording(h);
    sw(h, { type: 'run_state', state: 'running' });
    expect(q(h, 'stop').hidden).toBe(false);
    q(h, 'stop').click();
    expect(h.sent).toEqual([{ type: 'stop' }]);
    expect(recorder.cancelled).toBe(0);
    expect(h.view.getState().voice).toBe('recording');
    mic(h).click();
    expect(h.sent.at(-1)).toMatchObject({ type: 'voice.audio' });
  });

  it('Stop keeps working while transcribing', async () => {
    const h = setupVoice();
    await startRecording(h);
    mic(h).click();
    sw(h, { type: 'run_state', state: 'running' });
    q(h, 'stop').click();
    expect(h.sent.at(-1)).toEqual({ type: 'stop' });
    sw(h, { type: 'voice.result', request_id: 'voz-1', text: 'hola' });
    expect(input(h).value).toBe('hola');
  });
});
