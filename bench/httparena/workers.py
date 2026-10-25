import multiprocessing
import os


def worker_count() -> int:
    try:
        count = int(os.environ.get('WEB_WORKERS', ''))
    except ValueError:
        count = 0
    if count <= 0:
        try:
            count = len(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            try:
                count = multiprocessing.cpu_count()
            except NotImplementedError:
                count = 1
    return max(1, min(count, 128))
