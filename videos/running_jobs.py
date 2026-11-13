"""Process-local registry of running download threads.

Maps codecurl_id -> Thread for in-flight downloads.

shortcut: per-process only — a second gunicorn worker cannot see these
entries, so running-job protection and supersession only work within the
same worker. Upgrade to a shared store (Redis, DB row-level lock) when
the project runs multiple workers and needs cross-worker protection.
"""

import threading
from typing import Callable

_download_threads: dict[str, threading.Thread] = {}
_threads_lock = threading.Lock()


def register(codecurl_id: str, thread: threading.Thread) -> None:
    with _threads_lock:
        _download_threads[codecurl_id] = thread


def unregister(codecurl_id: str) -> None:
    with _threads_lock:
        _download_threads.pop(codecurl_id, None)


def is_running(codecurl_id: str) -> bool:
    with _threads_lock:
        return codecurl_id in _download_threads


def running_ids() -> list[str]:
    with _threads_lock:
        return list(_download_threads)


def register_if_absent(
    codecurl_id: str,
    thread: threading.Thread,
    *,
    while_locked: Callable[[], None] = lambda: None,
) -> bool:
    """Atomically check-and-register under the lock.

    If *codecurl_id* is already registered, return False without calling
    *while_locked*. Otherwise call *while_locked()* (still under the lock),
    then register and return True.

    Registering LAST ensures a failed *while_locked* (e.g. mark_started or
    thread.start) never leaves a stale entry; the worker's cleanup waits
    on this same lock, so it cannot pop before registration completes.
    """
    with _threads_lock:
        if codecurl_id in _download_threads:
            return False
        while_locked()
        _download_threads[codecurl_id] = thread
        return True
