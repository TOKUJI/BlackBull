"""Small primitives shared by resource-owning cleanup paths."""
import asyncio


async def cancel_and_wait(*tasks: asyncio.Task) -> None:
    """Cancel *tasks* and wait for them to end, dropping what they end with.

    A cancellation aimed at the caller during the wait still propagates, once
    the awaited task has ended: it is forwarded to that task, which may absorb
    it, but it raises the caller's cancel count, which the tasks' own does not.
    """
    caller = asyncio.current_task()
    cancelling = caller.cancelling()
    for task in tasks:
        task.cancel()
    for i, task in enumerate(tasks):
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        if caller.cancelling() > cancelling:
            for rest in tasks[i + 1:]:
                rest.add_done_callback(_retrieved)
            raise asyncio.CancelledError


def _retrieved(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


def combine_cleanup_errors(*errors):
    """Keep one error's identity; group distinct concurrent failures."""
    unique = []
    seen = set()
    for error in errors:
        if error is not None and id(error) not in seen:
            unique.append(error)
            seen.add(id(error))
    if len(unique) < 2:
        return unique[0] if unique else None
    return BaseExceptionGroup('cleanup failed', unique)
