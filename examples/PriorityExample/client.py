"""
HTTP/2 Priority Example — client
=================================
Demonstrates sending HTTP/2 priority hints to the BlackBull priority server.

Priority hints are sent as a standard 'priority' HTTP header (RFC 9218 §4.2).
httpx forwards this header over HTTP/2; a compliant intermediary or server
parses it and populates scope['http2_priority'] on the server side.

BlackBull also accepts PRIORITY_UPDATE frames (type 0x10) directly, but that
requires a lower-level HTTP/2 client.  The 'priority' header is the portable,
application-level approach.

Requirements:
    pip install 'httpx[http2]'   # pulls in h2

Run (server must already be started):
    python client.py [--base-url https://localhost:8443]
"""
import argparse
import asyncio
import httpx


BASE_URL = 'https://localhost:8443'


async def demo(base_url: str) -> None:
    # verify=False because the server uses a self-signed certificate.
    async with httpx.AsyncClient(http2=True, verify=False,
                                 base_url=base_url) as client:

        # ------------------------------------------------------------------ #
        # 1. /priority-echo — see what the server resolved                    #
        # ------------------------------------------------------------------ #
        print('=== /priority-echo ===')
        cases = [
            ('u=0',    'highest urgency (interactive)'),
            ('u=3',    'normal urgency (default)'),
            ('u=6, i', 'background prefetch, incremental'),
            ('u=7',    'lowest urgency (speculative)'),
        ]
        for field, label in cases:
            r = await client.get('/priority-echo',
                                 headers={'priority': field})
            r.raise_for_status()
            data = r.json()
            hint = data['http2_priority']
            print(f'  priority: {field!r:12s}  ({label})')
            print(f'    → urgency={hint["urgency"]}  '
                  f'incremental={hint["incremental"]}')
        print()

        # ------------------------------------------------------------------ #
        # 2. /work — observe how urgency affects response time                #
        # ------------------------------------------------------------------ #
        print('=== /work (variable-cost endpoint) ===')
        import time
        for urgency in (0, 3, 7):
            field = f'u={urgency}'
            t0 = time.monotonic()
            r = await client.get('/work', headers={'priority': field})
            r.raise_for_status()
            elapsed = (time.monotonic() - t0) * 1000
            data = r.json()
            print(f'  priority: {field!r:5s}  '
                  f'server delay={data["delay_ms"]:3d} ms  '
                  f'round-trip={elapsed:.0f} ms')
        print()

        print('Done.  Run the server with --help for options.')


def main() -> None:
    parser = argparse.ArgumentParser(description='Priority example client')
    parser.add_argument('--base-url', default=BASE_URL,
                        help='Base URL of the priority server '
                             f'(default: {BASE_URL})')
    args = parser.parse_args()
    asyncio.run(demo(args.base_url))


if __name__ == '__main__':
    main()
