/**
 * Scenario 2 — Constant-load stress test
 *
 * Applies a constant 500 VUs for 60 seconds.  Use this to find the
 * hard failure point: error rate, memory growth, and whether the server
 * recovers after the load ends.
 *
 * Run:
 *   k6 run bench/k6/http_stress.js
 *
 * To compare endpoints, override TARGET via env:
 *   k6 run -e TARGET=/64kb bench/k6/http_stress.js
 */
import http from 'k6/http';
import { check } from 'k6';
import { Rate } from 'k6/metrics';

const errorRate = new Rate('errors');

const TARGET = __ENV.TARGET || '/ping';

export const options = {
  vus:      500,
  duration: '60s',
  thresholds: {
    http_req_duration: ['p(99)<500'],
    errors:            ['rate<0.05'],
  },
};

const BASE   = 'https://localhost:8443';
const PARAMS = { insecureSkipTLSVerify: true };

export default function () {
  const res = http.get(`${BASE}${TARGET}`, PARAMS);
  errorRate.add(res.status !== 200);
  check(res, { 'status 200': r => r.status === 200 });
}

export function teardown() {
  const res = http.get(`${BASE}/metrics`, PARAMS);
  if (res.status === 200) {
    const m = JSON.parse(res.body);
    console.log(`Loop lag — p50: ${m.p50} ms  p99: ${m.p99} ms  max: ${m.max} ms`);
  }
}
