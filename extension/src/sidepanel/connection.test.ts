import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PanelToSw } from '../shared/messages';
import { PanelConnection, type PortLike } from './connection';

class FakePort implements PortLike {
  posted: unknown[] = [];
  messageListeners: Array<(m: unknown) => void> = [];
  disconnectListeners: Array<() => void> = [];
  failPost = false;
  onMessage = { addListener: (fn: (m: unknown) => void): void => void this.messageListeners.push(fn) };
  onDisconnect = { addListener: (fn: () => void): void => void this.disconnectListeners.push(fn) };
  postMessage(message: unknown): void {
    if (this.failPost) throw new Error('disconnected');
    this.posted.push(message);
  }
  disconnect(): void {}
  receive(message: unknown): void {
    for (const fn of this.messageListeners) fn(message);
  }
  drop(): void {
    for (const fn of this.disconnectListeners) fn();
  }
}

function setup(failConnect = 0) {
  const ports: FakePort[] = [];
  const messages: unknown[] = [];
  const links: boolean[] = [];
  let failures = failConnect;
  const connection = new PanelConnection({
    connect: () => {
      if (failures > 0) {
        failures -= 1;
        throw new Error('Extension context invalidated');
      }
      const port = new FakePort();
      ports.push(port);
      return port;
    },
    onMessage: (m) => messages.push(m),
    onLink: (up) => links.push(up),
  });
  return { connection, ports, messages, links };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('PanelConnection', () => {
  it('connects, announces the link and asks for the settings', () => {
    const { connection, ports, links } = setup();
    connection.start();
    expect(ports).toHaveLength(1);
    expect(links).toEqual([true]);
    expect(ports[0]?.posted).toEqual([{ type: 'settings.get' }]);
  });

  it('passes valid worker messages through and ignores anything else', () => {
    const { connection, ports, messages } = setup();
    connection.start();
    const port = ports[0] as FakePort;
    port.receive({ type: 'status', connected: true, halted: false, running: false });
    port.receive({ type: 'status', connected: 'yes' });
    port.receive({ type: 'nonsense' });
    port.receive('text');
    port.receive(null);
    port.receive({ type: 'confirm', request_id: '', title: 'x', lines: [], explanation: '' });
    expect(messages).toEqual([{ type: 'status', connected: true, halted: false, running: false }]);
  });

  it('reconnects with a growing delay after the port drops, and resets after a valid message', () => {
    const { connection, ports, links } = setup();
    connection.start();
    ports[0]?.drop();
    expect(links).toEqual([true, false]);
    vi.advanceTimersByTime(249);
    expect(ports).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(ports).toHaveLength(2);
    ports[1]?.drop();
    vi.advanceTimersByTime(499);
    expect(ports).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(ports).toHaveLength(3);
    ports[2]?.receive({ type: 'status', connected: true, halted: false, running: false });
    ports[2]?.drop();
    vi.advanceTimersByTime(250);
    expect(ports).toHaveLength(4);
    expect(links).toEqual([true, false, true, false, true, false, true]);
  });

  it('caps the delay and keeps retrying when connect throws', () => {
    const { connection, ports } = setup(8);
    connection.start();
    for (let i = 0; i < 20; i += 1) vi.advanceTimersByTime(5000);
    expect(ports).toHaveLength(1);
    expect(connection.connected).toBe(true);
  });

  it('re-asks for the settings on every new connection', () => {
    const { connection, ports } = setup();
    connection.start();
    ports[0]?.drop();
    vi.advanceTimersByTime(250);
    expect(ports[1]?.posted).toEqual([{ type: 'settings.get' }]);
  });

  it('sends while connected and refuses while disconnected', () => {
    const { connection, ports } = setup();
    connection.start();
    const message: PanelToSw = { type: 'resume' };
    expect(connection.send(message)).toBe(true);
    ports[0]?.drop();
    expect(connection.send(message)).toBe(false);
  });

  it('a stop that cannot be delivered is sent first on the next connection', () => {
    const { connection, ports } = setup();
    connection.start();
    ports[0]?.drop();
    expect(connection.send({ type: 'stop' })).toBe(true);
    vi.advanceTimersByTime(0);
    expect(ports).toHaveLength(2);
    expect(ports[1]?.posted).toEqual([{ type: 'stop' }, { type: 'settings.get' }]);
    ports[1]?.drop();
    vi.advanceTimersByTime(5000);
    expect(ports[2]?.posted).toEqual([{ type: 'settings.get' }]);
  });

  it('a stop on a port whose post throws is kept for the next one', () => {
    const { connection, ports } = setup();
    connection.start();
    (ports[0] as FakePort).failPost = true;
    expect(connection.send({ type: 'stop' })).toBe(true);
    (ports[0] as FakePort).drop();
    vi.advanceTimersByTime(5000);
    expect(ports[1]?.posted[0]).toEqual({ type: 'stop' });
  });

  it('does not reconnect after close', () => {
    const { connection, ports } = setup();
    connection.start();
    connection.close();
    ports[0]?.drop();
    vi.advanceTimersByTime(60_000);
    expect(ports).toHaveLength(1);
  });
});
