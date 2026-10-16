import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Bridge } from './bridge';

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readyState = FakeWebSocket.CONNECTING;
  sent: Array<Record<string, unknown>> = [];
  onopen: (() => Promise<void> | void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(JSON.parse(data));
  }

  close(): void {
    if (this.readyState === FakeWebSocket.CLOSED) return;
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }

  async open(): Promise<void> {
    this.readyState = FakeWebSocket.OPEN;
    await this.onopen?.();
  }

  welcome(): void {
    this.onmessage?.({ data: JSON.stringify({ v: 1, id: 'w', type: 'welcome', payload: { server_version: '0.2.0' } }) });
  }

  hellos(): number {
    return this.sent.filter((message) => message.type === 'hello').length;
  }
}

async function flush(): Promise<void> {
  for (let turn = 0; turn < 5; turn++) await Promise.resolve();
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.useFakeTimers();
  vi.stubGlobal('WebSocket', FakeWebSocket);
  vi.stubGlobal('chrome', {
    storage: { local: { get: async () => ({}) } },
    runtime: { getManifest: () => ({ version: '0.2.0' }) },
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('Bridge connection', () => {
  it('opens a single socket when connect() is called twice at install time', async () => {
    const bridge = new Bridge();
    await Promise.all([bridge.connect(), bridge.connect()]);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('sends the hello on the socket that opened', async () => {
    const bridge = new Bridge();
    await Promise.all([bridge.connect(), bridge.connect()]);
    const [socket] = FakeWebSocket.instances;
    await socket.open();
    expect(socket.hellos()).toBe(1);
  });

  it('is connected after the welcome and disconnected when the server closes', async () => {
    const bridge = new Bridge();
    const seen: boolean[] = [];
    bridge.onStatus((connected) => seen.push(connected));
    await bridge.connect();
    const [socket] = FakeWebSocket.instances;
    await socket.open();
    socket.welcome();
    expect(bridge.connected).toBe(true);
    socket.close();
    expect(bridge.connected).toBe(false);
    expect(seen).toEqual([true, false]);
  });

  it('reconnects with backoff after the server closes the socket', async () => {
    const bridge = new Bridge();
    await bridge.connect();
    await FakeWebSocket.instances[0].open();
    FakeWebSocket.instances[0].close();
    expect(FakeWebSocket.instances).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(500);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it('ignores the late close of a socket that was already replaced', async () => {
    const bridge = new Bridge();
    await bridge.connect();
    const [first] = FakeWebSocket.instances;
    await first.open();
    first.welcome();
    const closeFirst = first.close.bind(first);
    first.close = () => undefined;
    await bridge.reconnect();
    const second = FakeWebSocket.instances[1];
    await second.open();
    second.welcome();
    expect(bridge.connected).toBe(true);
    closeFirst();
    expect(bridge.connected).toBe(true);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it('does not let a stale socket mark the bridge as connected', async () => {
    const bridge = new Bridge();
    await bridge.connect();
    const [first] = FakeWebSocket.instances;
    first.close = () => undefined;
    await bridge.reconnect();
    first.welcome();
    expect(bridge.connected).toBe(false);
  });

  it('keeps one socket when reconnect() runs while connect() is still starting', async () => {
    const bridge = new Bridge();
    const starting = bridge.connect();
    const restarted = bridge.reconnect();
    await Promise.all([starting, restarted]);
    await flush();
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('keeps the keepalive of the live socket when a stale one closes', async () => {
    const bridge = new Bridge();
    await bridge.connect();
    const [first] = FakeWebSocket.instances;
    first.close = () => undefined;
    const closeFirst = (): void => {
      first.readyState = FakeWebSocket.CLOSED;
      first.onclose?.();
    };
    await bridge.reconnect();
    const second = FakeWebSocket.instances[1];
    await second.open();
    second.welcome();
    closeFirst();
    await vi.advanceTimersByTimeAsync(20_000);
    expect(second.sent.filter((message) => message.type === 'ping')).toHaveLength(1);
  });
});
