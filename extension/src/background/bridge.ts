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
  private handlers = new Map<string, RequestHandler>();
  private statusListeners = new Set<StatusListener>();
  private helloExtras: () => Promise<Record<string, unknown>> = async () => ({});
  connected = false;
  serverVersion = '';

  on(type: string, handler: RequestHandler): void {
    this.handlers.set(type, handler);
  }

  onStatus(listener: StatusListener): void {
    this.statusListeners.add(listener);
  }

  setHelloExtras(fn: () => Promise<Record<string, unknown>>): void {
    this.helloExtras = fn;
  }

  async connect(): Promise<void> {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) return;
    clearTimeout(this.reconnectTimer);
    const port = await getBridgePort();
    let ws: WebSocket;
    try {
      ws = new WebSocket(`ws://127.0.0.1:${port}/`);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = async () => {
      this.backoff = MIN_BACKOFF_MS;
      const extras = await this.helloExtras();
      this.send({
        v: PROTOCOL_VERSION,
        id: newId(),
        type: 'hello',
        payload: { ext_version: chrome.runtime.getManifest().version, protocol: PROTOCOL_VERSION, ...extras },
      });
      clearInterval(this.keepalive);
      this.keepalive = setInterval(() => this.emit('ping', {}), KEEPALIVE_MS);
    };
    ws.onmessage = (event) => {
      void this.onMessage(event.data);
    };
    ws.onclose = () => {
      clearInterval(this.keepalive);
      if (this.ws === ws) this.ws = null;
      this.setConnected(false);
      this.scheduleReconnect();
    };
    ws.onerror = () => undefined;
  }

  async reconnect(): Promise<void> {
    this.ws?.close();
    this.ws = null;
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
