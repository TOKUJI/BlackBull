/**
 * Scenario 3 — WebSocket echo round-trip latency
 *
 * Opens 50 concurrent WebSocket connections; each sends a timestamped
 * message every 200 ms and measures the echo round-trip time.
 *
 * Run:
 *   k6 run bench/k6/websocket.js
 */
import ws from 'k6/ws';
import { check } from 'k6';
import { Trend, Counter } from 'k6/metrics';

const wsRTT      = new Trend('ws_rtt_ms', true);
const wsMessages = new Counter('ws_messages');
const wsErrors   = new Counter('ws_errors');

export const options = {
  vus:      50,
  duration: '60s',
  thresholds: {
    ws_rtt_ms: ['p(95)<50', 'p(99)<100'],
    ws_errors: ['count<10'],
  },
};

const URL    = 'wss://localhost:8443/ws';
const PARAMS = { 'insecureSkipTLSVerify': true };

export default function () {
  const res = ws.connect(URL, PARAMS, function (socket) {
    let pending = {};  // id → sent timestamp

    socket.on('open', () => {
      // Send a ping every 200 ms for the duration of the connection
      socket.setInterval(() => {
        const id  = Date.now().toString();
        pending[id] = Date.now();
        socket.send(id);
      }, 200);
    });

    socket.on('message', (msg) => {
      const t0 = pending[msg];
      if (t0 !== undefined) {
        wsRTT.add(Date.now() - t0);
        wsMessages.add(1);
        delete pending[msg];
      }
    });

    socket.on('error', (e) => {
      wsErrors.add(1);
      console.error('WS error:', e);
    });

    // Close after 10 seconds; k6 will loop if duration > 10s
    socket.setTimeout(() => socket.close(), 10000);
  });

  check(res, { 'connected': r => r && r.status === 101 });
}
