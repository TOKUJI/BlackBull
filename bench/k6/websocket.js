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
  insecureSkipTLSVerify: true,
  vus:      50,
  duration: '60s',
  thresholds: {
    ws_rtt_ms: ['p(95)<50', 'p(99)<100'],
    ws_errors: ['count<10'],
  },
};

// WS_URL takes precedence; otherwise derive from BASE (the HTTP base
// the harness already passes through) by swapping the scheme so split-
// topology runs hit wss://bench-server.internal:8443/ws transparently.
const URL    = __ENV.WS_URL ||
    (__ENV.BASE
        ? __ENV.BASE.replace(/^http(s?):/, 'ws$1:') + '/ws'
        : 'wss://localhost:8443/ws');
const PARAMS = {};

// Sequence counter for unique message IDs (the old Date.now()-as-id
// collides when two pings land in the same millisecond — frequent on a
// loopback). RTT is still measured in ms; k6's WS VM does not expose
// performance.now() (ReferenceError), and there is no high-resolution
// timer in this context. On WSL2 loopback most RTTs are <1 ms so p50
// usually reports 0 — interpret this lane as throughput first
// (msg/s) and tail second.
let _seq = 0;

export default function () {
  const res = ws.connect(URL, PARAMS, function (socket) {
    let pending = {};  // id → sent timestamp (ms)

    socket.on('open', () => {
      socket.setInterval(() => {
        const id = '' + (++_seq);
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
