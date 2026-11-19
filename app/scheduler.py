"""Background worker: the only place that triggers NS calls.

Loop: evaluate every device (this records which NS data is needed and how
fresh it should be), make one NS call for the most overdue item, evaluate
again with the new data. ns_client.fetch_next() keeps calls at least
ns_client.CALL_INTERVAL seconds apart (NS_CALLS_PER_MINUTE), whatever happens.
"""
import threading
import time

from . import ns_client, state

_thread = None
_stop = threading.Event()
_wake = threading.Event()


def wake():
    """Something changed (new route/settings): look for work right away."""
    _wake.set()


def run_once(wait=True):
    state.evaluate_all()
    ns_client.want(ns_client.STATIONS_PATH, ns_client.STATIONS_MAX_AGE)
    fetched = ns_client.fetch_next(wait=wait)
    if fetched:
        state.evaluate_all()
    return fetched


def _loop():
    while not _stop.is_set():
        try:
            _wake.clear()
            if run_once() is None:
                _wake.wait(5)  # nothing due right now (or until woken)
        except Exception as exc:
            print("Scheduler error:", str(exc))
            _stop.wait(10)


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="ns-scheduler", daemon=True)
    _thread.start()


def stop():
    _stop.set()
    _wake.set()
