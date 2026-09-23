"""Server child for ``test_accept_admission``.

argv: ``<RLIMIT_NOFILE> <backlog> [startup-park-seconds]``.
"""
import asyncio
import json
import os
import resource
import sys

SOFT = int(sys.argv[1])
BACKLOG = sys.argv[2]
# Before ``import blackbull``: the cap is derived from RLIMIT_NOFILE when the
# settings are first built.
resource.setrlimit(resource.RLIMIT_NOFILE,
                   (SOFT, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
os.environ['BB_MAX_CONNECTIONS'] = 'auto'
os.environ['BB_SOCKET_BACKLOG'] = BACKLOG

from blackbull import BlackBull                                       # noqa: E402
from blackbull.env import (reset_settings_cache,                      # noqa: E402
                           resolve_max_connections)
from blackbull.server.server import Server                            # noqa: E402

reset_settings_cache()
cap = resolve_max_connections('auto')

PARK = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0

app = BlackBull()


@app.on_startup
async def _park():
    if PARK:
        await asyncio.sleep(PARK)


@app.route(path='/')
async def index():
    return 'ok'


ACCEPT_RESOURCE = 'out of system resource'

record = {'accept_errors': 0, 'other_loop_errors': 0}


def _count(_loop, context):
    if ACCEPT_RESOURCE in context.get('message', ''):
        record['accept_errors'] += 1
    else:
        record['other_loop_errors'] += 1


async def main():
    asyncio.get_running_loop().set_exception_handler(_count)
    server = Server(app, max_connections=cap)
    server.open_socket(port=0)
    print(json.dumps({'port': server.port, 'cap': cap, 'soft': SOFT}), flush=True)
    runner = asyncio.ensure_future(server.run())

    await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)

    print(json.dumps({**record, 'active': server._active_connections}), flush=True)
    await server.stop(drain_timeout=1.0)
    try:
        await asyncio.wait_for(runner, 10)
    except (Exception, asyncio.CancelledError):
        pass


asyncio.run(main())
