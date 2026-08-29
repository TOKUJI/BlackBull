#!/usr/bin/env python3
"""Option C — 統合 NativeResponse + DX properties（header/body view）。

model C からの拡張:
- `resp.header` が `_HeaderView` を返す（BlackBull Headers と同系の DX:
  get / append / getlist / contains / len / iter）。ゼロコピー view で、
  middleware の変更は sender に見える。
- `resp.body` は property。補助 DX として content_length / is_empty /
  content_type を追加。
- status / more_body / trailers はホットパスなので素の属性のまま
  （property 化しない — 性能劣化を避ける）。
- compression の None 破壊バグを修正（header は header を持つ応答にだけ付与）。
- 末尾に性能比較（素の属性 vs property+view）のマイクロベンチマーク。

実行: python3 send-model-c.py
"""
from __future__ import annotations
import asyncio
import time


# --- ゼロコピー header view（BlackBull Headers の DX を最小再現）---------------
class _HeaderView:
    __slots__ = ('_items',)

    def __init__(self, items: list[tuple[bytes, bytes]]):
        self._items = items

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, name: bytes) -> bool:
        return any(k == name for k, _ in self._items)

    def get(self, name: bytes, default: bytes = b'') -> bytes:
        for k, v in self._items:
            if k == name:
                return v
        return default

    def getlist(self, name: bytes) -> list[tuple[bytes, bytes]]:
        return [(k, v) for k, v in self._items if k == name]

    def append(self, name_or_pairs, value: bytes | None = None) -> None:
        if value is None:
            self._items.extend(name_or_pairs)
        else:
            self._items.append((name_or_pairs, value))


# --- 統合 native 応答オブジェクト（__slots__ + DX property）--------------------
class NativeResponse:
    __slots__ = ('status', '_header', '_body', 'more_body', 'trailers')

    def __init__(self, *, status: int = 200,
                 header: list[tuple[bytes, bytes]] | None = None,
                 body: bytes | None = None,
                 more_body: bool = False,
                 trailers: list[tuple[bytes, bytes]] | None = None):
        self.status = status
        self.header = header          # setter が _header に保存
        self._body = body
        self.more_body = more_body
        self.trailers = trailers

    # --- header: DX view（無ければ None — presence は None で判定）---
    @property
    def header(self):
        if self._header is None:
            return None
        return _HeaderView(self._header)

    @header.setter
    def header(self, value):
        if value is None:
            self._header = None
        elif isinstance(value, _HeaderView):
            self._header = value._items
        else:
            self._header = value

    # --- body: 素の bytes（DX は補助 property で）---
    @property
    def body(self) -> bytes | None:
        return self._body

    @body.setter
    def body(self, value: bytes | None) -> None:
        self._body = value

    @property
    def content_length(self) -> int:
        return len(self._body) if self._body is not None else 0

    @property
    def is_empty(self) -> bool:
        return self._body is None or self._body == b''

    @property
    def content_type(self) -> bytes:
        hv = self.header
        return hv.get(b'content-type') if hv is not None else b''

    def to_asgi(self) -> list[dict]:
        events: list[dict] = []
        if self._header is not None:
            events.append({'type': 'http.response.start',
                           'status': self.status, 'headers': self._header})
        if self._body is not None:
            events.append({'type': 'http.response.body',
                           'body': self._body, 'more_body': self.more_body})
        if self.trailers is not None:
            events.append({'type': 'http.response.trailers',
                           'headers': self.trailers})
        return events


# --- サーバー側 sender --------------------------------------------------------
class H1Sender:
    def __init__(self) -> None:
        self.wire: list[bytes] = []
        self._pending: bytes | None = None

    async def __call__(self, resp: NativeResponse) -> None:
        hv = resp.header          # property → view（None なら None）
        if hv is not None:
            head = f'HTTP/1.1 {resp.status} OK\r\n'.encode()
            for k, v in hv:
                head += k + b': ' + v + b'\r\n'
            self._pending = head + b'\r\n'
        if resp.body is not None:
            if self._pending is not None:
                self.wire.append(self._pending)
                self._pending = None
            self.wire.append(resp.body)
        if resp.trailers is not None:
            self.wire.append(b'\r\n' + b''.join(b'%b: %b\r\n' % (k, v)
                                                for k, v in resp.trailers))
        if not resp.more_body and self._pending is not None:
            self.wire.append(self._pending)
            self._pending = None


# --- middleware --------------------------------------------------------------
def logging_middleware(send):
    async def wrapped(resp: NativeResponse) -> None:
        print(f'  [log] header={"yes" if resp.header is not None else "no"} '
              f'body={"yes" if resp.body is not None else "no"} '
              f'len={resp.content_length} ct={resp.content_type!r}')
        await send(resp)
    return wrapped


def compression(send):
    """None 保存を厳守: header は header を持つ応答にだけ付与（ストリーミングで
    重複ステータス行を出さない）。body チャンクは header=None のまま。"""
    async def wrapped(resp: NativeResponse) -> None:
        if resp.body is not None and resp.body:
            new_header = None
            if resp._header is not None:
                new_header = resp._header + [(b'content-encoding', b'gzip')]
            resp = NativeResponse(status=resp.status, header=new_header,
                                  body=b'[gzip]' + resp.body,
                                  more_body=resp.more_body,
                                  trailers=resp.trailers)
        await send(resp)
    return wrapped


# --- handler ----------------------------------------------------------------
async def handler(send) -> None:
    await send(NativeResponse(status=200,
                              header=[(b'content-type', b'text/plain')],
                              body=b'Hello, world!'))


async def streaming_handler(send) -> None:
    await send(NativeResponse(status=200,
                              header=[(b'content-type', b'text/plain')]))
    await send(NativeResponse(body=b'chunk1', more_body=True))
    await send(NativeResponse(body=b'chunk2'))


# --- DX デモ（middleware が property で操作）----------------------------------
async def dx_demo() -> None:
    print('--- DX demo: middleware reads/writes via properties ---')
    sender = H1Sender()
    send = logging_middleware(compression(sender))
    resp = NativeResponse(header=[(b'content-type', b'text/plain')],
                          body=b'Hello')
    # middleware が view 経由で header を操作
    assert resp.header is not None
    resp.header.append(b'x-extra', b'from-dx')
    print(f'  resp.content_type={resp.content_type!r}  '
          f'resp.content_length={resp.content_length}  '
          f'resp.is_empty={resp.is_empty}  '
          f'x-extra={resp.header.get(b"x-extra")!r}')
    await send(resp)


async def run(label: str, send_factory, h) -> None:
    sender = H1Sender()
    print(f'--- {label} ---')
    await h(send_factory(sender))
    print(f'  wire: {b"".join(sender.wire)!r}\n')


# --- 性能比較: 素の属性 vs property+view（send パス N 回）----------------------
def bench() -> None:
    N = 200_000
    resp = NativeResponse(header=[(b'content-type', b'text/plain')],
                          body=b'Hello, world!')

    # 素の属性で送信（property 無し相当）
    def send_plain(r):
        h = r._header
        head = f'HTTP/1.1 {r.status} OK\r\n'.encode()
        for k, v in h:
            head += k + b': ' + v + b'\r\n'
        out = head + b'\r\n' + (r._body if r._body is not None else b'')
        return out

    # property + view で送信（実装）
    def send_prop(r):
        hv = r.header
        head = f'HTTP/1.1 {r.status} OK\r\n'.encode()
        for k, v in hv:
            head += k + b': ' + v + b'\r\n'
        out = head + b'\r\n' + (r.body if r.body is not None else b'')
        return out

    # warmup
    for _ in range(10_000):
        send_plain(resp)
        send_prop(resp)

    t0 = time.perf_counter()
    for _ in range(N):
        send_plain(resp)
    t1 = time.perf_counter()
    for _ in range(N):
        send_prop(resp)
    t2 = time.perf_counter()

    plain_ns = (t1 - t0) / N * 1e9
    prop_ns = (t2 - t1) / N * 1e9
    print('--- microbench: send path (per response) ---')
    print(f'  plain attrs : {plain_ns:8.1f} ns')
    print(f'  prop + view : {prop_ns:8.1f} ns')
    print(f'  delta       : {prop_ns - plain_ns:+8.1f} ns  '
          f'({(prop_ns - plain_ns) / plain_ns * 100:+6.1f} %)')
    print(f'  of 33 µs/req budget: {(prop_ns - plain_ns) / 33000 * 100:+.3f} %')


async def main() -> None:
    await run('complete / bare', lambda s: s, handler)
    await run('complete / logging', logging_middleware, handler)
    await run('complete / compression', compression, handler)
    await run('streaming / bare', lambda s: s, streaming_handler)
    await run('streaming / compression', compression, streaming_handler)
    await dx_demo()
    print()
    bench()


if __name__ == '__main__':
    asyncio.run(main())

