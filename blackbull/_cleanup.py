"""Small primitives shared by resource-owning cleanup paths."""
import asyncio


async def cancel_and_wait(task: asyncio.Task) -> None:
    """Cancel *task* and wait for it to end, dropping what it ends with.

    A cancellation aimed at the caller during the wait still propagates;
    awaiting *task* itself would take it for the task's own.
    """
    task.cancel()
    task.add_done_callback(_retrieved)
    await asyncio.wait({task})


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
