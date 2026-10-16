import {
  DEFAULT_BRIDGE_PORT,
  PROTOCOL_VERSION,
  isEnvelope,
  newId,
  toErrorInfo,
  type Envelope,
  type ErrorInfo,
  type ResultMessage,
} from '../shared/protocol';

export type RequestHandler = (payload: Record<string, unknown>) => Promise<Record<string, unknown> | void>;
type StatusListener = (connected: boolean) => void;
type WelcomeListener = (payload: Record<string, unknown>) => void;

const KEEPALIVE_MS = 20_000;
const MIN_BACKOFF_MS = 500;
const MAX_BACKOFF_MS = 5_000;

export async function getBridgePort(): Promise<number> {
  const { bridgePort } = await chrome.storage.local.get('bridgePort');
  const port = Number(bridgePort);
  return Number.isInteger(port) && port > 0 && port < 65536 ? port : DEFAULT_BRIDGE_PORT;
}

export class Bridge {
  private ws: WebSocket | null = null;
  private keepalive: ReturnType<typeof setInterval> | undefined;
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  private backoff = MIN_BACKOFF_MS;
  private epoch = 0;
  private handlers = new Map<string, RequestHandler>();
  private statusListeners = new Set<StatusListener>();
  private welcomeListeners = new Set<WelcomeListener>();
  private helloExtras: () => Promise<Record<string, unknown>> = async () => ({});
  connected = false;
  serverVersion = '';

  on(type: string, handler: RequestHandler): void {
    this.handlers.set(type, handler);
  }

  onStatus(listener: StatusListener): void {
    this.statusListeners.add(listener);
  }

  onWelcome(listener: WelcomeListener): void {
    this.welcomeListeners.add(listener);
  }

  setHelloExtras(fn: () => Promise<Record<string, unknown>>): void {
    this.helloExtras = fn;
  }

  private get alive(): boolean {
    return this.ws !== null && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING);
  }

  async connect(): Promise<void> {
    if (this.alive) return;
    clearTimeout(this.reconnectTimer);
    const epoch = this.epoch;
    const port = await getBridgePort();
    // Two callers (onInstalled and the top-level call) can both get here while the port is being read.
    if (epoch !== this.epoch || this.alive) return;
    let ws: WebSocket;
    try {
      ws = new WebSocket(`ws://127.0.0.1:${port}/`);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = async () => {
      if (this.ws !== ws) return;
      this.backoff = MIN_BACKOFF_MS;
      const extras = await this.helloExtras().catch(() => ({}));
      if (this.ws !== ws || ws.readyState !== WebSocket.OPEN) return;
      ws.send(
        JSON.stringify({
          v: PROTOCOL_VERSION,
          id: newId(),
          type: 'hello',
          payload: { ext_version: chrome.runtime.getManifest().version, protocol: PROTOCOL_VERSION, ...extras },
        }),
      );
      clearInterval(this.keepalive);
      this.keepalive = setInterval(() => this.emit('ping', {}), KEEPALIVE_MS);
    };
    ws.onmessage = (event) => {
      if (this.ws === ws) void this.onMessage(event.data);
    };
    ws.onclose = () => {
      // A socket that was already replaced must not tear down the live one.
      if (this.ws !== ws) return;
      clearInterval(this.keepalive);
      this.ws = null;
      this.setConnected(false);
      this.scheduleReconnect();
    };
    ws.onerror = () => undefined;
  }

  async reconnect(): Promise<void> {
    this.epoch++;
    const old = this.ws;
    this.ws = null;
    clearInterval(this.keepalive);
    old?.close();
    this.setConnected(false);
    await this.connect();
  }

  emit(type: string, payload: Record<string, unknown>): void {
    this.send({ v: PROTOCOL_VERSION, id: newId(), type, payload });
  }

  private scheduleReconnect(): void {
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => void this.connect(), this.backoff);
    this.backoff = Math.min(this.backoff * 2, MAX_BACKOFF_MS);
  }

  private setConnected(value: boolean): void {
    if (this.connected === value) return;
    this.connected = value;
    for (const listener of this.statusListeners) listener(value);
  }

  private send(message: Envelope | ResultMessage): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(message));
  }

  private reply(id: string, ok: boolean, payload?: Record<string, unknown>, error?: ErrorInfo): void {
    const message: ResultMessage = ok
      ? { v: PROTOCOL_VERSION, id, type: 'result', ok: true, payload: payload ?? {} }
      : { v: PROTOCOL_VERSION, id, type: 'result', ok: false, error };
    this.send(message);
  }

  private async onMessage(raw: unknown): Promise<void> {
    let message: unknown;
    try {
      message = JSON.parse(String(raw));
    } catch {
      return;
    }
    if (!isEnvelope(message)) return;
    if (message.type === 'welcome') {
      this.serverVersion = String(message.payload?.server_version ?? '');
      for (const listener of this.welcomeListeners) {
        try {
          listener(message.payload ?? {});
        } catch {
          // a listener must not break the handshake
        }
      }
      this.setConnected(true);
      return;
    }
    const handler = this.handlers.get(message.type);
    if (!handler) {
      this.reply(message.id, false, undefined, { code: 'BAD_REQUEST', message: `Unknown request type: ${message.type}` });
      return;
    }
    try {
      const result = await handler(message.payload ?? {});
      this.reply(message.id, true, result ?? {});
    } catch (err) {
      this.reply(message.id, false, undefined, toErrorInfo(err));
    }
  }
}
