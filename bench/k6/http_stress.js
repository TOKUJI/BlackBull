/**
 * Scenario 2 — Constant-load stress test
 *
 * Applies a constant `K6_VUS` virtual users (default 500) for
 * `K6_DURATION` (default 60s).  Use this to find the hard failure
 * point: error rate, memory growth, and whether the server recovers
 * after the load ends.
 *
 * Run:
 *   k6 run bench/k6/http_stress.js
 *
 * Env overrides (all optional):
 *   K6_VUS=N         number of virtual users (default 500)
 *   K6_DURATION=Ns   constant-load window (default '60s')
 *   TARGET=/path     endpoint to hit (default '/ping')
 */
import http from 'k6/http';
import { check } from 'k6';
import { Rate } from 'k6/metrics';

const errorRate = new Rate('errors');
const h2Rate    = new Rate('http2_ok');  // 1 when response was served over HTTP/2

const TARGET   = __ENV.TARGET      || '/ping';
const VUS      = __ENV.K6_VUS      ? parseInt(__ENV.K6_VUS, 10) : 500;
const DURATION = __ENV.K6_DURATION || '60s';

export const options = {
  insecureSkipTLSVerify: true,
  vus:      VUS,
  duration: DURATION,
  thresholds: {
    http_req_duration: ['p(99)<500'],
    errors:            ['rate<0.05'],
    http2_ok:          ['rate>0.99'],
  },
};

const BASE   = 'https://localhost:8443';
const PARAMS = {};

export default function () {
  const res = http.get(`${BASE}${TARGET}`, PARAMS);
  errorRate.add(res.status !== 200);
  check(res, {
    'status 200': r => r.status === 200,
    'HTTP/2':     r => r.proto === 'HTTP/2.0',
  });
  h2Rate.add(res.proto === 'HTTP/2.0');
}

export function teardown() {
  const res = http.get(`${BASE}/metrics`, PARAMS);
  if (res.status === 200) {
    const m = JSON.parse(res.body);
    console.log(`Loop lag — p50: ${m.p50} ms  p99: ${m.p99} ms  max: ${m.max} ms`);
  }
}
