"""Log server — receives JSON access log records and stores them in SQLite.

Usage:
    python log_server.py            # listens on localhost:9000

Query stored records:
    sqlite3 logs.db "SELECT * FROM access_logs ORDER BY id DESC LIMIT 10;"
"""

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST = 'localhost'
PORT = 9000
DB_PATH = 'logs.db'

_lock = threading.Lock()   # serialise concurrent writes from multiple connections


def _init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS access_logs (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at     REAL    NOT NULL,
                client_ip      TEXT,
                method         TEXT,
                path           TEXT,
                http_version   TEXT,
                status         TEXT,
                response_bytes INTEGER,
                duration_ms    REAL,
                message        TEXT
            )
        ''')
        conn.commit()


class _LogHandler(BaseHTTPRequestHandler):

    def do_POST(self) -> None:
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)

        try:
            rec = json.loads(body)
        except json.JSONDecodeError as exc:
            print(f'[log_server] bad JSON: {exc}')
            self._respond(400)
            return

        try:
            with _lock, sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    '''INSERT INTO access_logs
                       (created_at, client_ip, method, path,
                        http_version, status, response_bytes, duration_ms, message)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (
                        rec.get('created'),
                        rec.get('client_ip', '-'),
                        rec.get('method',    '-'),
                        rec.get('path',      '-'),
                        rec.get('http_version', '-'),
                        str(rec.get('status', '-')),
                        rec.get('response_bytes', 0),
                        rec.get('duration_ms',    0.0),
                        rec.get('message',        ''),
                    ),
                )
                conn.commit()

            print(f"[log_server] stored: {rec.get('method')} {rec.get('path')}"
                  f" → {rec.get('status')}")
            self._respond(200)

        except Exception as exc:
            print(f'[log_server] DB error: {exc}')
            self._respond(500)

    def _respond(self, status: int) -> None:
        self.send_response(status)
        self.end_headers()

    # Silence the HTTP server's own per-request output
    def log_message(self, fmt, *args) -> None:  # type: ignore[override]
        pass


if __name__ == '__main__':
    _init_db()
    server = HTTPServer((HOST, PORT), _LogHandler)
    print(f'Log server listening on http://{HOST}:{PORT}')
    print(f'Records stored in {DB_PATH}')
    print('Press Ctrl-C to stop.\n')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
