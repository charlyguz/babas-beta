import { isSwMessage, type PanelToSw, type SwToPanel } from '../shared/messages';

export interface PortLike {
  postMessage(message: unknown): void;
  disconnect(): void;
  onMessage: { addListener(listener: (message: unknown) => void): void };
  onDisconnect: { addListener(listener: () => void): void };
}

export interface ConnectionOptions {
  connect: () => PortLike;
  onMessage: (message: SwToPanel) => void;
  onLink: (up: boolean) => void;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
  delaysMs?: readonly number[];
}

const DEFAULT_DELAYS_MS = [250, 500, 1000, 2000, 5000] as const;

export class PanelConnection {
  private port: PortLike | null = null;
  private timer: unknown = null;
  private attempt = 0;
  private stopQueued = false;
  private closed = false;
  private readonly delays: readonly number[];
  private readonly setTimer: (fn: () => void, ms: number) => unknown;
  private readonly clearTimer: (handle: unknown) => void;

  constructor(private readonly options: ConnectionOptions) {
    this.delays = options.delaysMs ?? DEFAULT_DELAYS_MS;
    this.setTimer = options.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = options.clearTimer ?? ((handle) => clearTimeout(handle as ReturnType<typeof setTimeout>));
  }

  get connected(): boolean {
    return this.port !== null;
  }

  start(): void {
    this.closed = false;
    this.open();
  }

  close(): void {
    this.closed = true;
    if (this.timer !== null) this.clearTimer(this.timer);
    this.timer = null;
    const port = this.port;
    this.port = null;
    try {
      port?.disconnect();
    } catch {
      // the port is gone already
    }
  }

  // A stop that cannot be delivered is kept and sent first on the next connection: Stop must never be lost.
  send(message: PanelToSw): boolean {
    if (this.post(message)) return true;
    if (message.type === 'stop') {
      this.stopQueued = true;
      this.scheduleReconnect(true);
      return true;
    }
    return false;
  }

  private post(message: PanelToSw): boolean {
    if (!this.port) return false;
    try {
      this.port.postMessage(message);
      return true;
    } catch {
      return false;
    }
  }

  private open(): void {
    if (this.closed || this.port) return;
    let port: PortLike;
    try {
      port = this.options.connect();
    } catch {
      this.scheduleReconnect(false);
      return;
    }
    this.port = port;
    port.onMessage.addListener((raw) => {
      if (this.port !== port || !isSwMessage(raw)) return;
      this.attempt = 0;
      this.options.onMessage(raw);
    });
    port.onDisconnect.addListener(() => {
      if (this.port !== port) return;
      this.port = null;
      this.options.onLink(false);
      this.scheduleReconnect(false);
    });
    this.options.onLink(true);
    if (this.stopQueued) {
      this.stopQueued = false;
      this.post({ type: 'stop' });
    }
    this.post({ type: 'settings.get' });
  }

  private scheduleReconnect(immediate: boolean): void {
    if (this.closed || this.port) return;
    if (this.timer !== null) {
      if (!immediate) return;
      this.clearTimer(this.timer);
    }
    const delay = immediate ? 0 : (this.delays[Math.min(this.attempt, this.delays.length - 1)] ?? 5000);
    if (!immediate) this.attempt += 1;
    this.timer = this.setTimer(() => {
      this.timer = null;
      this.open();
    }, delay);
  }
}
