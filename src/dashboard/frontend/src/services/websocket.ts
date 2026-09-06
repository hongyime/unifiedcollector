import { WS_BASE } from "../utils/constants";

export class HealthWebSocket {
  private ws: WebSocket | null = null;
  private reconnectAttempts = 0;
  private listeners = new Set<(data: unknown) => void>();

  connect() {
    if (this.ws?.readyState === WebSocket.OPEN) return;
    try {
      this.ws = new WebSocket(`${WS_BASE}/ws/health`);
      this.ws.onopen = () => { this.reconnectAttempts = 0; };
      this.ws.onmessage = (e) => {
        const data = JSON.parse(e.data);
        this.listeners.forEach((fn) => fn(data));
      };
      this.ws.onclose = () => this.scheduleReconnect();
      this.ws.onerror = () => this.ws?.close();
    } catch {
      this.scheduleReconnect();
    }
  }

  private scheduleReconnect() {
    if (this.reconnectAttempts >= 10) return;
    const delay = Math.min(1000 * 2 ** this.reconnectAttempts, 30000);
    this.reconnectAttempts++;
    setTimeout(() => this.connect(), delay);
  }

  subscribe(fn: (data: unknown) => void) {
    this.listeners.add(fn);
    return () => { this.listeners.delete(fn); };
  }

  disconnect() {
    this.ws?.close();
    this.ws = null;
  }
}

export const healthWS = new HealthWebSocket();
