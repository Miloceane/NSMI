"""Background workers: the only place that triggers API calls.

One worker thread per provider (NS, SNCF), so a provider that is waiting
for its budget never delays another. Each loop: evaluate that provider's
devices (this records which data is needed and how fresh it should be),
make one call for the most overdue item, evaluate again with the new data.
Api.fetch_next() keeps calls at least `call_interval` seconds apart
(NS_CALLS_PER_MINUTE / SNCF_CALLS_PER_MINUTE), whatever happens.
"""
import threading

from . import accounts, providers, state

_threads = {}
_stop = threading.Event()
_wake = {name: threading.Event() for name in providers.PROVIDERS}


def wake():
    """Something changed (new route/settings): look for work right away."""
    for event in _wake.values():
        event.set()


def _in_use(provider):
    return any(providers.of_account(a).name == provider.name for a in accounts.load().values())


def run_provider(provider, wait=True):
    if not _in_use(provider):
        return None  # no devices: no calls at all (not even the station list)
    state.evaluate_all(provider)
    for path, max_age in provider.background_paths():
        provider.api.want(path, max_age)
    fetched = provider.api.fetch_next(wait=wait)
    if fetched:
        state.evaluate_all(provider)
    return fetched


def run_once(wait=False):
    """One round for every provider (used by tests)."""
    results = [run_provider(p, wait) for p in providers.PROVIDERS.values()]
    return next((r for r in results if r), None)


def _loop(provider):
    event = _wake[provider.name]
    while not _stop.is_set():
        try:
            event.clear()
            if run_provider(provider, wait=False) is None:
                # Nothing due, or the budget doesn't allow a call yet: look
                # again soon (or when woken). Devices keep being re-evaluated,
                # so stale data still turns the light white.
                delay = provider.api.seconds_until_call_allowed()
                event.wait(min(5, max(0.5, delay)) if delay else 5)
        except Exception as exc:
            print(f"{provider.label} scheduler error:", str(exc))
            _stop.wait(10)


def start():
    _stop.clear()
    for name, provider in providers.PROVIDERS.items():
        thread = _threads.get(name)
        if thread and thread.is_alive():
            continue
        thread = threading.Thread(
            target=_loop, args=(provider,), name=f"{name}-scheduler", daemon=True)
        _threads[name] = thread
        thread.start()


def stop():
    _stop.set()
    wake()
