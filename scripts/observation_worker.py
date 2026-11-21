"""Build-time worker entry point for baseline observations in reusable process pools."""

import os
import sys


def execute(mask, payload):
    from multiprocessing.reduction import ForkingPickler
    import observations as observer

    observer.begin_child()
    observer.active_test = mask
    observer.seen.clear()
    function, args, kwargs = ForkingPickler.loads(payload)
    return function(*args, **kwargs)


def track_submissions(observer):
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing.reduction import ForkingPickler

    original = ProcessPoolExecutor.submit

    def submit(executor, fn, /, *args, **kwargs):
        payload = bytes(ForkingPickler.dumps((fn, args, kwargs)))
        observer.starting_pool = True
        try:
            return original(executor, execute, observer.active_test, payload)
        finally:
            observer.starting_pool = False

    ProcessPoolExecutor.submit = submit
