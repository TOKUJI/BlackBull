/**
 * WebSocket echo *throughput* lane for the four-row A/B harness
 * (bench/peers/ab_commit_ws.sh).
 *
 * Saturation shape: each VU opens one connection, then offers messages far
 * faster than any server can echo them (BURST messages every TICK_MS).  The
 * server's echo rate — `ws_echoed` — is the measured axis; because the client
 * always offers more than the server can return, the number the server's
 * read/echo path actually completes is the throughput.
 *
 * Two pacing knobs exist so the offered load can be raised until the server
 * is demonstrably the bottleneck (echoed ≈ sent with low latency, then
 * echoed < sent once saturated).  The *same* script runs on both A/B arms,
 * so the offered load is byte-identical between them.
 *
 * Sockets close after WS_LIFETIME_MS so iterations and the run's graceful
 * stop stay short (the legacy `k6/ws` API has no explicit send-buffer
 * backpressure, so a short lifetime also bounds client-side buffering).
 *
 * Run:
 *   k6 run bench/k6/websocket_echo_throughput.js
 * Env:
 *   WS_URL      ws://host:port/ws   (default ws://127.0.0.1:8443/ws)
 *   WS_VUS      concurrent connections (default 100)
 *   WS_DURATION run length            (default 20s)
 *   WS_BURST    messages per tick     (default 8)
 *   WS_TICK_MS  tick period           (default 2)
 *   WS_LIFETIME_MS socket lifetime    (default 5000)
 */
import ws from 'k6/ws';

import { Counter } from 'k6/metrics';

const wsEchoed = new Counter('ws_echoed');
const wsSent   = new Counter('ws_sent');
const wsErrors = new Counter('ws_errors');

const URL = __ENV.WS_URL || 'ws://127.0.0.1:8443/ws';
const VUS = Number(__ENV.WS_VUS || 100);
const DURATION = __ENV.WS_DURATION || '20s';
const BURST = Number(__ENV.WS_BURST || 8);
const TICK_MS = Number(__ENV.WS_TICK_MS || 2);
const LIFETIME_MS = Number(__ENV.WS_LIFETIME_MS || 5000);

export const options = {
  vus: VUS,
  duration: DURATION,
  thresholds: {
    ws_errors: ['count<10'],
  },
};

export default function () {
  ws.connect(URL, {}, function (socket) {
    socket.on('open', () => {
      socket.setInterval(() => {
        for (let i = 0; i < BURST; i++) {
          wsSent.add(1);
          socket.send('m');
        }
      }, TICK_MS);
    });
    socket.on('message', () => {
      wsEchoed.add(1);
    });
    socket.on('error', () => {
      wsErrors.add(1);
    });
    socket.setTimeout(() => socket.close(), LIFETIME_MS);
  });
}
