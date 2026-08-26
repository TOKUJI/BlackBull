"""Hammer one split offset repeatedly, at several split delays.

The companion to ``validate_frag.py``, which sweeps every offset once.  One
pass per offset cannot tell "fixed" from "got lucky", and the failure mode
these checks look for is a **silently wrong body**, not an error — a flaky
pass and a correct one are the same green line.

It also shows why the delay matters: at 0 ms the kernel often coalesces the
two writes, so no fragmentation happens and a broken server looks fine.

Usage:  frag_repeat.py <port> [runs] [offset]
"""
import socket, sys, time

HOST = '127.0.0.1'
REQ = ("POST /baseline11?a=13&b=42 HTTP/1.1\r\n"
       "Host: 127.0.0.1\r\nContent-Type: text/plain\r\n"
       "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
       "2\r\n20\r\n0\r\n\r\n").encode()
OFFSET = 133          # between the '2' and the '0' of the chunk data
EXPECT = b'75'


def one(port, delay):
    s = socket.create_connection((HOST, port), timeout=10)
    try:
        s.sendall(REQ[:OFFSET])
        time.sleep(delay)
        s.sendall(REQ[OFFSET:])
        buf = b''
        s.settimeout(10)
        while True:
            c = s.recv(65536)
            if not c:
                break
            buf += c
        head, _, body = buf.partition(b'\r\n\r\n')
        status = int(head.split(b' ')[1]) if head else -1
        if b'chunked' in head.lower():
            out, rest = b'', body
            while True:
                size, _, rest = rest.partition(b'\r\n')
                n = int(size.split(b';')[0] or b'0', 16)
                if not n:
                    break
                out, rest = out + rest[:n], rest[n+2:]
            body = out
        return status, body.strip()
    finally:
        s.close()


def sweep(label, port, n, delays):
    print(f'=== {label} (port {port}) ===')
    for d in delays:
        bad = []
        for i in range(n):
            try:
                st, body = one(port, d)
            except Exception as e:
                bad.append(f'exc {type(e).__name__}'); continue
            if st != 200 or body != EXPECT:
                bad.append(f'{st} {body!r}')
        verdict = 'all correct' if not bad else f'{len(bad)}/{n} WRONG -> {sorted(set(bad))}'
        print(f'  delay={d*1000:>4.0f}ms  n={n:<4} {verdict}')


port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
n = int(sys.argv[2]) if len(sys.argv) > 2 else 40
if len(sys.argv) > 3:
    OFFSET = int(sys.argv[3])
sweep(f'port {port}', port, n, [0.0, 0.005, 0.05, 0.2])
