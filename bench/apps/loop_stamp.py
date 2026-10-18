"""Loop identity stamp for Phase 2 (Sprint 100).

Writes the row's *effective* running loop class to ``BB_LOOP_STAMP_OUT`` once
at import — the identity proof the Phase 1 artifacts lacked (the ``BB_UVLOOP``
row setting was only visible in the launch env, never in the report).

The effective class is derived from the row's configuration, deterministically:
- BlackBull: ``BB_UVLOOP=1`` AND uvloop importable → ``uvloop.LoopPolicy``,
  else ``asyncio.DefaultEventLoopPolicy``.  (``blackbull.env`` installs the
  uvloop policy at ``asyncio.run()`` time, so the runtime policy is not visible
  at app import — the config determines it exactly.)
- sanic: uvloop importable → ``uvloop.LoopPolicy`` (sanic sets the policy at
  import), else ``asyncio.DefaultEventLoopPolicy``.

Observation-only; off by default.  The per-stack file path is set by
``compare_servers.sh`` (``BB_LOOP_STAMP_OUT``), which also stamps the value
into ``compare_servers.md``.
"""
import importlib.util
import json
import os

_OUT = os.environ.get("BB_LOOP_STAMP_OUT", "")
if _OUT:
    uvloop_ok = importlib.util.find_spec("uvloop") is not None
    bb_uvloop = os.environ.get("BB_UVLOOP", "")
    if bb_uvloop != "":
        loop_class = (
            "uvloop.LoopPolicy"
            if (bb_uvloop == "1" and uvloop_ok)
            else "asyncio.DefaultEventLoopPolicy"
        )
    else:
        loop_class = (
            "uvloop.LoopPolicy" if uvloop_ok else "asyncio.DefaultEventLoopPolicy"
        )
    try:
        with open(_OUT, "w") as fh:
            json.dump(
                {
                    "pid": os.getpid(),
                    "effective_loop": loop_class,
                    "bb_uvloop_env": bb_uvloop,
                    "uvloop_importable": uvloop_ok,
                },
                fh,
            )
    except OSError:
        pass
