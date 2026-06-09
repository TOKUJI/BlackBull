"""Launcher wrapper that allows py-spy to attach without sudo.

YAMA's ptrace_scope=1 restricts attachment to ancestors of the target
process.  Sibling processes (py-spy started from a separate shell) get
EPERM.  ``prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY)`` opts this
process into accepting attachments from any same-user process.

Used by bench/static_repro for local profiling only; this wrapper is
never imported by BlackBull itself.
"""
import ctypes
import ctypes.util
import os
import sys

# prctl constants (see <sys/prctl.h>)
PR_SET_PTRACER = 0x59616d61   # 1499557217
PR_SET_PTRACER_ANY = ctypes.c_ulong(-1).value


def _allow_any_ptracer() -> None:
    libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
    rc = libc.prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY, 0, 0, 0)
    if rc != 0:
        errno = ctypes.get_errno()
        sys.stderr.write(
            f'WARNING: prctl(PR_SET_PTRACER) failed (errno={errno}) — '
            f'py-spy attach from another shell may be blocked\n'
        )


def _install_task_dumper() -> None:
    """SIGUSR1 → print every alive asyncio task's coroutine + frame.

    Lets a separate shell trigger an on-demand snapshot of all
    suspended coroutines (sockets in CLOSE_WAIT correspond to
    suspended connection-actor tasks).  py-spy only shows the
    currently-running coroutine on the main thread.
    """
    import asyncio
    import signal
    import traceback

    def _coro_chain_frames(coro, max_depth: int = 50):
        """Walk the cr_await chain to collect ALL suspended frames.

        ``Task.get_stack()`` only returns the outer coroutine's frame.
        For ``await inner_coro()`` chains, the inner coroutine's frame
        sits on the cr_await side of the outer coroutine.  Follow that
        link recursively so we can see which inner ``await`` is the
        actual suspension point.
        """
        frames = []
        seen = set()
        cur = coro
        for _ in range(max_depth):
            if cur is None or id(cur) in seen:
                break
            seen.add(id(cur))
            # cr_frame for coroutines, gi_frame for generator-based.
            frame = getattr(cur, 'cr_frame', None) or getattr(cur, 'gi_frame', None)
            if frame is not None:
                frames.append(frame)
            # cr_await points to whatever this coro is awaiting.
            cur = getattr(cur, 'cr_await', None) or getattr(cur, 'gi_yieldfrom', None)
        return frames

    def _dump(_signum, _frame):
        try:
            loop = asyncio.get_event_loop()
            tasks = list(asyncio.all_tasks(loop=loop))
        except RuntimeError:
            tasks = []
        sys.stderr.write(f'\n=== SIGUSR1 task dump: {len(tasks)} alive tasks ===\n')
        buckets: dict[tuple, int] = {}
        for t in tasks:
            try:
                coro = t.get_coro()
                frames = _coro_chain_frames(coro)
                if frames:
                    key = tuple(
                        f'{f.f_code.co_filename.replace("/home/toshio/BlackBull/", "")}'
                        f':{f.f_lineno} in {f.f_code.co_name}'
                        for f in frames
                    )
                else:
                    key = ('(no frames)',)
            except Exception as e:
                key = (f'(err {e!r})',)
            buckets[key] = buckets.get(key, 0) + 1
        for k, count in sorted(buckets.items(), key=lambda x: -x[1]):
            sys.stderr.write(f'  ----- {count} tasks -----\n')
            for frame_str in k:
                sys.stderr.write(f'    {frame_str}\n')
        sys.stderr.write('=== end ===\n')
        sys.stderr.flush()

    signal.signal(signal.SIGUSR1, _dump)


if __name__ == '__main__':
    _allow_any_ptracer()
    _install_task_dumper()
    # Hand off to bench/httparena/app.py with the same argv.
    here = os.path.dirname(os.path.abspath(__file__))
    app_py = os.path.normpath(os.path.join(here, '..', 'httparena', 'app.py'))
    # Re-exec via runpy so __name__ == '__main__' in app.py and its
    # argparse picks up our argv tail.
    import runpy
    sys.argv = [app_py] + sys.argv[1:]
    runpy.run_path(app_py, run_name='__main__')
