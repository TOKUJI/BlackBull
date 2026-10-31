"""Small primitives shared by resource-owning cleanup paths."""
import asyncio


async def cancel_and_wait(*tasks: asyncio.Task) -> None:
    """Cancel *tasks* and wait for them to end, dropping what they end with.

    A cancellation aimed at the caller during the wait still propagates: it
    raises the caller's cancel count, which the tasks' own cancellation does
    not, and it may reach the awaited task instead, which can absorb it.
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
