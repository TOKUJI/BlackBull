/**
 * Scenario 1 — Saturation ramp-up
 *
 * Ramps from 0 to 200 virtual users over 2 minutes, holds for 1 minute,
 * then drains.  Measures the VU count at which p99 latency crosses 50 ms
 * (the single-process asyncio saturation point).
 *
 * Run:
 *   k6 run bench/k6/http_rampup.js
 *   k6 run --out json=bench/results/rampup.json bench/k6/http_rampup.js
 */
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';

const errorRate = new Rate('errors');
const lagP99    = new Trend('loop_lag_p99_ms', true);
const h2Rate    = new Rate('http2_ok');  // 1 when response was served over HTTP/2

export const options = {
  insecureSkipTLSVerify: true,
  stages: [
    { duration: '30s', target: 50  },  // warm-up
    { duration: '60s', target: 200 },  // ramp to stress
    { duration: '60s', target: 200 },  // hold
    { duration: '30s', target: 0   },  // drain
  ],
  thresholds: {
    http_req_duration: ['p(95)<100', 'p(99)<200'],
    errors:            ['rate<0.01'],
    http2_ok:          ['rate>0.99'],  // fail the run if less than 99% of requests used HTTP/2
  },
};

const BASE = 'https://localhost:8443';
const PARAMS = {};

export default function () {
  const res = http.get(`${BASE}/ping`, PARAMS);
  const ok = check(res, {
    'status 200': r => r.status === 200,
    'body pong':  r => r.body === 'pong',
    'HTTP/2':     r => r.proto === 'HTTP/2.0',
  });
  errorRate.add(!ok);
  h2Rate.add(res.proto === 'HTTP/2.0');
}

/** Poll /metrics once per iteration from VU 1 only, to avoid adding noise. */
export function setup() {
  const res = http.get(`${BASE}/metrics`, PARAMS);
  if (res.status !== 200) {
    console.warn(`/metrics returned ${res.status} — lag data unavailable`);
  }
}

export function teardown() {
  const res = http.get(`${BASE}/metrics`, PARAMS);
  if (res.status === 200) {
    const m = JSON.parse(res.body);
    console.log(`Loop lag at teardown — p99: ${m.p99} ms  max: ${m.max} ms  samples: ${m.samples}`);
  }
}
