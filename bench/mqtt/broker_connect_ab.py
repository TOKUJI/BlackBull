"""ABBA A/B of the MQTT broker's per-CONNECT handling cost — BLA-357.

Arms are full copies of ``blackbull/mqtt/broker.py`` — ``base`` is e4f3985,
``pr`` is 129bd7f, ``fast`` is the working tree — loaded as siblings of
the package, so the arms differ only in that module (the revisions
differ in no other ``blackbull/`` file).
``null`` is a second load of ``base``: an A/A pair measured in the same
session, so every delta gets the run's own noise floor beside it.

One operation is one ``BrokerActor._on_attach(conn, connect)`` on a fresh
fake connection whose ``send()`` stores the message and touches it the
way production does: once through ``blackbull.mqtt.connection._output_size``
(mailbox enqueue accounting) and once through ``Send.wire_bytes()`` (the
write path's cache read).  CONNECT packets are built outside the timed
region.  The
broker is built once per arm and scenario at its baseline — 0 or 1000
live sessions squatting ``auto-`` names — by default ``auto-1..auto-1000``
(the counter-era attack; only a sequential allocator can be forced to
walk it — pass ``random`` for names drawn in the current allocator's
format) — and
each operation restores the slots an attach touches (session-table
entries; the base arm's counter), so the timed work is the changed code,
not fixture churn (a seeded entry an attach overwrites is replaced, not
dropped).

    UV_CACHE_DIR=/tmp/uv-cache uv run --no-sync python bench/mqtt/broker_connect_ab.py [rounds] [ops]

Deltas are round-paired: each round measures every arm once in a rotated
order and reports pr-base, fast-pr and fast-base per round (positive
means the later arm is slower), so drift common to a round cancels.  The
95% CI is the t-interval over those paired differences.  Per-round arm
values and paired deltas print with every scenario, so each reported
number recomputes from the output alone.  Verdict rule: a comparison is
a regression only when the whole CI sits above the A/A pair's own CI
spread (the noise floor).
"""
from __future__ import annotations

import asyncio
import gc
import importlib.util
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORK = REPO.parent / '.bench-bla357'
sys.path.insert(0, str(REPO))          # run as a script, import as the package
ARMS = ('base', 'pr', 'fast', 'null')
BASE_REV = 'e4f3985'
PR_REV = '129bd7f'
SCENARIOS = (
    'empty-id', 'named-id', 'empty-id-limit-ok', 'named-id-limit-ok',
    'empty-id-limit-refuse', 'empty-id-1k-sessions', 'named-id-1k-sessions',
)
NAMED_ID = 'bench-client'
SEED_SESSIONS = 1000

_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
        8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160,
        14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093,
        20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
        26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042}


from blackbull.mqtt.connection import _output_size  # noqa: E402
from blackbull.mqtt.broker import Send  # noqa: E402


def load(name: str, path: Path):
    # Import the package first: the arms resolve `.messages` and `..actor`
    # through `blackbull.mqtt.__path__`, which only exists once it is in.
    import blackbull.mqtt  # noqa: F401
    import blackbull.mqtt.broker as shared
    spec = importlib.util.spec_from_file_location(f'blackbull.mqtt.{name}', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    # The diff does not touch Send/Close: alias them to the checkout's
    # classes so the shared _output_size accounts for the arms' messages.
    module.Send = shared.Send
    module.Close = shared.Close
    return module


def snapshot() -> None:
    WORK.mkdir(exist_ok=True)
    for arm, rev in (('base', BASE_REV), ('pr', PR_REV)):
        text = subprocess.run(
            ['git', '-C', str(REPO), 'show', f'{rev}:blackbull/mqtt/broker.py'],
            capture_output=True, check=True).stdout
        (WORK / f'broker_{arm}.py').write_bytes(text)
    (WORK / 'broker_fast.py').write_bytes(
        (REPO / 'blackbull/mqtt/broker.py').read_bytes())


class FakeConn:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, msg) -> None:
        _output_size(msg)               # the Mailbox charges this at enqueue
        if isinstance(msg, Send):
            msg.wire_bytes()            # the write path reads the cache again
        self.sent.append(msg)


def seed_names(mode: str) -> tuple[str, ...]:
    # Deterministic for pairing across arms and rounds.
    if mode == 'random':
        rng = random.Random(0xB1A357)
        names = tuple(f'auto-{rng.getrandbits(48):012x}'
                      for _ in range(SEED_SESSIONS))
    else:
        names = tuple(f'auto-{index}'
                      for index in range(1, SEED_SESSIONS + 1))
    assert len(set(names)) == SEED_SESSIONS
    return names


SEED_NAMES = seed_names('counter')


def broker_for(mod, scenario: str):
    broker = mod.BrokerActor()
    if scenario.endswith('1k-sessions'):
        for name in SEED_NAMES:
            # _expires_at None: live sessions the sweep must examine and keep.
            broker._sessions[name] = mod._new_broker_session()
    return broker


def seed_keys_for(scenario: str) -> frozenset[str]:
    return (frozenset(SEED_NAMES)
            if scenario.endswith('1k-sessions') else frozenset())


def connect_for(scenario: str, refuse_limit: int, ok_limit: int):
    from blackbull.mqtt.messages import MQTTConnect
    named = scenario.startswith('named-id')
    props = {}
    if scenario.endswith('limit-ok'):
        props['maximum_packet_size'] = ok_limit
    elif scenario.endswith('limit-refuse'):
        props['maximum_packet_size'] = refuse_limit
    return MQTTConnect(client_id=NAMED_ID if named else '', clean_start=True,
                       keep_alive=60, properties=props)


async def one_op(broker, connect, seed_keys, seed_template) -> None:
    conn = FakeConn()
    await broker._on_attach(conn, connect)
    client_id = broker._client_by_conn.pop(id(conn), None)
    if client_id is not None:
        # The base arm can register a squatted auto-N over its seed entry;
        # restore that entry rather than dropping it, or a long run would
        # erode the 1k baseline only for the arms that collide with it.
        if client_id in seed_keys:
            broker._sessions[client_id] = seed_template
        else:
            broker._sessions.pop(client_id, None)
        broker._clients.pop(client_id, None)
    if hasattr(broker, '_auto_seq'):
        broker._auto_seq = 0        # the base arm still counts; keep ids constant


async def time_arm(broker, connect, seed_keys, seed_template,
                   ops: int) -> float:
    gc.disable()
    try:
        start = time.perf_counter()
        for _ in range(ops):
            await one_op(broker, connect, seed_keys, seed_template)
        return (time.perf_counter() - start) / ops * 1e9
    finally:
        gc.enable()


def ci95(samples: list[float]) -> float:
    if len(samples) < 2:
        return float('nan')
    t = _T95.get(len(samples) - 1, 2.0)
    return t * statistics.stdev(samples) / len(samples) ** 0.5


async def probe_limits(mods) -> tuple[int, int]:
    """Mechanically set the per-limit scenario bounds: the refuse limit is
    one byte below the smallest success CONNACK among the assigning arms,
    so every arm that honours the limit refuses; the accept limit clears
    the largest, and the bare refusal must still fit.  The base CONNACK
    differs by design: it carries no assigned Client Identifier; base and
    null must agree (A/A)."""
    from blackbull.mqtt.messages import (
        MQTTConnack, MQTTConnect, ReasonCode, encode_packet)
    sizes = {}
    for arm in ARMS:
        for _ in range(2):
            conn = FakeConn()
            await mods[arm].BrokerActor()._on_attach(
                conn, MQTTConnect(client_id='', clean_start=True, keep_alive=60))
            size = len(conn.sent[0].wire_bytes())
            assert sizes.setdefault(arm, size) == size, (
                f'{arm} CONNACK size varies across draws: {sizes[arm]} vs {size}')
    assert sizes['base'] == sizes['null'], f'CONNACK sizes differ: {sizes}'
    success = min(sizes['pr'], sizes['fast'])
    reject = len(encode_packet(MQTTConnack(
        session_present=False, reason_code=ReasonCode.PACKET_TOO_LARGE)))
    refuse_limit, ok_limit = success - 1, 1024
    assert reject <= refuse_limit < success <= max(sizes.values()) <= ok_limit, (
        f'reject={reject} refuse={refuse_limit} sizes={sizes} ok={ok_limit}')
    print(f'CONNACK sizes {sizes}, reject {reject} bytes')
    return refuse_limit, ok_limit


def report(scenario: str, seen: dict[str, list[float]],
           pair: dict[str, list[float]]) -> None:
    floor = ci95(pair['base-null'])
    print(f'{"pair":30} {"mean d":>10} {"+-95% CI":>10} {"floor":>8}  verdict')
    for name in ('pr-base', 'fast-pr', 'fast-base'):
        samples = pair[name]
        delta, ci = statistics.mean(samples), ci95(samples)
        verdict = 'regression' if delta - ci > floor else 'no-regression'
        print(f'{scenario + " " + name:30} {delta:10.1f} {ci:10.1f} '
              f'{floor:8.1f}  {verdict}')
    print(f'{scenario + " base-null":30} '
          f'{statistics.mean(pair["base-null"]):10.1f} {floor:10.1f}')


async def main(rounds: int, ops: int, seed_mode: str = 'counter') -> None:
    global SEED_NAMES
    SEED_NAMES = seed_names(seed_mode)
    snapshot()
    mods = {arm: load(f'_ab_{arm}',
                      WORK / f'broker_{"base" if arm == "null" else arm}.py')
            for arm in ARMS}
    refuse_limit, ok_limit = await probe_limits(mods)

    print(f'{rounds} rounds x {ops} ops; per-op nanoseconds; seed {seed_mode}')
    for scenario in SCENARIOS:
        connect = connect_for(scenario, refuse_limit, ok_limit)
        brokers = {arm: broker_for(mods[arm], scenario) for arm in ARMS}
        seeds = {arm: (seed_keys_for(scenario), mods[arm]._new_broker_session())
                 for arm in ARMS}
        for _ in range(3):               # warm each arm before measuring
            for arm in ARMS:
                await time_arm(brokers[arm], connect, *seeds[arm], 200)
        seen = {arm: [] for arm in ARMS}
        pair = {'pr-base': [], 'fast-pr': [], 'fast-base': [], 'base-null': []}
        for round_index in range(rounds):
            order = list(ARMS[round_index % len(ARMS):]) \
                + list(ARMS[:round_index % len(ARMS)])
            for arm in order:
                seen[arm].append(
                    await time_arm(brokers[arm], connect, *seeds[arm], ops))
            pair['pr-base'].append(seen['pr'][-1] - seen['base'][-1])
            pair['fast-pr'].append(seen['fast'][-1] - seen['pr'][-1])
            pair['fast-base'].append(seen['fast'][-1] - seen['base'][-1])
            pair['base-null'].append(seen['base'][-1] - seen['null'][-1])
        print(f'\n{scenario}: '
              + '  '.join(f'{arm} {statistics.mean(seen[arm]):.1f}'
                          for arm in ARMS))
        print(f'{"round":>5}  {"base":>12} {"pr":>12} {"fast":>12} '
              f'{"null":>12}  {"pr-base":>9} {"fast-pr":>9} '
              f'{"fast-base":>9} {"base-null":>9}')
        for index in range(rounds):
            print(f'{index + 1:>5}  {seen["base"][index]:>12.3f} '
                  f'{seen["pr"][index]:>12.3f} {seen["fast"][index]:>12.3f} '
                  f'{seen["null"][index]:>12.3f}  '
                  f'{pair["pr-base"][index]:>9.3f} '
                  f'{pair["fast-pr"][index]:>9.3f} '
                  f'{pair["fast-base"][index]:>9.3f} '
                  f'{pair["base-null"][index]:>9.3f}')
        report(scenario, seen, pair)


if __name__ == '__main__':
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 15,
                     int(sys.argv[2]) if len(sys.argv) > 2 else 20_000,
                     sys.argv[3] if len(sys.argv) > 3 else 'counter'))
