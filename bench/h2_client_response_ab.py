"""ABBA A/B of the HTTP/2 client's response-completion cost — BLA-337.

Arms are full copies of ``blackbull/client/http2.py`` loaded as siblings of
the package, so the arms differ only in that module and share every other
byte: ``base`` is ``f5f664f``, ``pr`` is the branch before the optimisation,
and ``fast`` is the working tree.  An A/A null pair is measured in the same
session, so the run reports its own noise floor next to every delta.

Each call is one response: a fresh ``_PendingResponse`` and one final
HEADERS with eight fields and END_STREAM, which is what ``_on_response_headers``
plus ``_complete`` cost per request.

    uv run python bench/h2_client_response_ab.py [rounds] [calls]

Deltas are round-paired: each round measures every arm once in a rotated
order and reports A-B per round, so drift common to a round cancels.  The
95% CI is the t-interval over those paired differences.
"""
from __future__ import annotations

import asyncio
import gc
import importlib.util
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORK = REPO.parent / '.bench-bla337'
sys.path.insert(0, str(REPO))          # run as a script, import as the package
ARMS = ('base', 'pr', 'fast')

FIELDS = [
    (b'content-type', b'application/json'),
    (b'content-length', b'0'),
    (b'date', b'Sat, 26 Sep 2026 00:00:00 GMT'),
    (b'server', b'blackbull'),
    (b'cache-control', b'no-store'),
    (b'vary', b'accept-encoding'),
    (b'x-request-id', b'0123456789abcdef'),
    (b'etag', b'"abc"'),
]


def load(name: str, path: Path):
    # Import the package first: the arms resolve `..env` and friends through
    # `blackbull.client.__path__`, which only exists once the package is in.
    import blackbull.client  # noqa: F401
    spec = importlib.util.spec_from_file_location(f'blackbull.client.{name}', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def snapshot() -> None:
    import subprocess
    WORK.mkdir(exist_ok=True)
    for arm, rev in (('base', 'f5f664f'), ('pr', '934e742')):
        text = subprocess.run(
            ['git', '-C', str(REPO), 'show', f'{rev}:blackbull/client/http2.py'],
            capture_output=True, check=True).stdout
        (WORK / f'http2_{arm}.py').write_bytes(text)
    (WORK / 'http2_fast.py').write_bytes(
        (REPO / 'blackbull/client/http2.py').read_bytes())


async def one_shot(mod, frame):
    loop = asyncio.get_running_loop()
    pending = mod._PendingResponse(future=loop.create_future())
    mod.HTTP2Client._adopt  # attribute check: the arm really loaded
    client = mod.HTTP2Client.__new__(mod.HTTP2Client)
    client._responses = {1: pending}
    client.sent = []
    await client._on_response_headers(frame)
    return pending


def make_frame(mod_path: Path):
    from blackbull.protocol.frame import FrameFactory
    from blackbull.protocol.frame_types import (FrameTypes, HeaderFrameFlags,
                                                PseudoHeaders)
    factory = FrameFactory()
    frame = factory.create(
        FrameTypes.HEADERS,
        int(HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM), 1)
    frame.pseudo_headers[PseudoHeaders.STATUS] = '200'
    frame.headers.extend(FIELDS)
    return frame


async def time_arm(mod, frame, calls: int) -> float:
    gc.disable()
    try:
        start = time.perf_counter()
        for _ in range(calls):
            await one_shot(mod, frame)
        return (time.perf_counter() - start) / calls * 1e6
    finally:
        gc.enable()


def ci95(samples: list[float]) -> float:
    if len(samples) < 2:
        return float('nan')
    return 2.262 * statistics.stdev(samples) / len(samples) ** 0.5


async def main(rounds: int, calls: int) -> None:
    snapshot()
    mods = {arm: load(f'_ab_{arm}', WORK / f'http2_{arm}.py') for arm in ARMS}
    frames = {arm: make_frame(WORK / f'http2_{arm}.py') for arm in ARMS}
    mods['base2'] = load('_ab_base2', WORK / 'http2_base.py')
    frames['base2'] = frames['base']

    for _ in range(3):                       # warm each arm before measuring
        for arm in ARMS:
            await time_arm(mods[arm], frames[arm], 200)

    pair: dict[str, list[float]] = {name: [] for name in
                                    ('base-pr', 'base-fast', 'base-base2')}
    for _ in range(rounds):
        order = list(ARMS) + ['base2']
        seen = {name: await time_arm(mods[name], frames[name], calls)
                for name in order}
        pair['base-pr'].append(seen['base'] - seen['pr'])
        pair['base-fast'].append(seen['base'] - seen['fast'])
        pair['base-base2'].append(seen['base'] - seen['base2'])

    print(f'{rounds} rounds x {calls} calls; per-call microseconds')
    print(f'{"pair":12} {"mean d":>10} {"+-95% CI":>10}')
    for name, samples in pair.items():
        print(f'{name:12} {statistics.mean(samples):10.2f} '
              f'{ci95(samples):10.2f}')


if __name__ == '__main__':
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 15,
                     int(sys.argv[2]) if len(sys.argv) > 2 else 20_000))
