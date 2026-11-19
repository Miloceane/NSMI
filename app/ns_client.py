"""NS API access: a shared cache in front of a strict rate limiter.

Nothing in the app calls NS directly. Code that needs NS data calls
`ns_get(path)`, which only reads the cache. If the answer isn't cached
yet it raises NeedData and remembers the path as wanted. The background
worker (app/scheduler.py) calls `fetch_next()`, which performs at most
ONE real HTTP call per NS_CALL_INTERVAL seconds (default 60), for the
wanted path that needs it most. The time of the last call is stored on
disk so restarts can't cause bursts.

Because the cache is keyed by URL, devices with the same route share their
trip data and everyone shares the disruption feed.
"""
import difflib
import os
import threading
import time

import httpx

from . import storage
from .text import normalize

NS_BASE = "https://gateway.apiportal.ns.nl/reisinformatie-api/api"
# NS allows 300 calls / 5 min (1 per second). We use a small, fixed share of
# that: calls are spaced at least CALL_INTERVAL seconds apart, always.
CALLS_PER_MINUTE = max(1, min(30, int(os.getenv("NS_CALLS_PER_MINUTE", "6"))))
CALL_INTERVAL = 60 / CALLS_PER_MINUTE
DEFAULT_MAX_AGE = 60          # refetch trips/disruptions when older than this
STATIONS_MAX_AGE = 86400      # station names: once a day
FORGET_AFTER = 30 * 60        # drop paths nobody asked for in this long
STATIONS_PATH = "/v2/stations?countryCodes=NL"
LAST_CALL_FILE = "ns_last_call.json"


class NeedData(Exception):
    """The answer isn't cached yet; the worker will fetch it soon."""


_lock = threading.Lock()
_call_lock = threading.Lock()
_cache = {}   # path -> {"data", "fetched", "error", "attempted", "wanted", "max_age"}
_used = threading.local()
_round = {"n": 0}
_blocked_until = {"t": 0.0}   # set by a 429 from NS


def new_round():
    """Called before every device is re-evaluated (see scheduler)."""
    _round["n"] += 1


# ------------------------------------------------------------ cache reads

def ns_get(path, max_age=None, track=True):
    """`track=False`: don't count this data towards data_age() (used for
    slow-changing data such as the station list)."""
    if max_age is None:
        max_age = getattr(_used, "max_age", DEFAULT_MAX_AGE)
    now = time.time()
    with _lock:
        entry = _cache.setdefault(path, {"attempted": 0})
        entry["wanted"] = now
        # Shortest interval asked for in this round of evaluations wins.
        if entry.get("round") != _round["n"]:
            entry["round"] = _round["n"]
            entry["max_age"] = max_age
        else:
            entry["max_age"] = min(max_age, entry["max_age"])
        if "data" in entry:
            oldest = getattr(_used, "oldest", None)
            if track and oldest is not None:
                _used.oldest = min(oldest, entry["fetched"])
            return entry["data"]
        error = entry.get("error")

    if error:
        raise RuntimeError(error)
    raise NeedData(path)


def track_age(max_age=DEFAULT_MAX_AGE):
    """Start recording how old the data read by this thread is, and ask for
    data read from now on to be refreshed every `max_age` seconds."""
    _used.oldest = float("inf")
    _used.max_age = max_age


def data_age():
    """Seconds since the oldest data read since track_age() was fetched."""
    oldest = getattr(_used, "oldest", float("inf"))
    return 0 if oldest == float("inf") else time.time() - oldest


# ------------------------------------------------------------ fetching

def _next_path(now):
    with _lock:
        for path in [p for p, e in _cache.items() if now - e.get("wanted", 0) > FORGET_AFTER]:
            del _cache[path]

        due = [
            (path, e) for path, e in _cache.items()
            if "data" not in e or now - e["fetched"] >= e["max_age"]
        ]
        if not due:
            return None
        # Missing answers first, then the most overdue relative to how
        # fresh it should be (an urgent route 2 min old beats a quiet one
        # 11 min old).
        def overdue(e):
            return (now - max(e.get("fetched", 0), e["attempted"])) / e["max_age"]

        due = [(p, e) for p, e in due if now - e["attempted"] >= min(e["max_age"], 60)]
        if not due:
            return None
        due.sort(key=lambda item: ("data" in item[1], -overdue(item[1])))
        return due[0][0]


def _last_call():
    try:
        return float(storage.read_json(LAST_CALL_FILE)["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def seconds_until_call_allowed(now=None):
    now = now if now is not None else time.time()
    return max(0.0, _last_call() + CALL_INTERVAL - now, _blocked_until["t"] - now)


def _http_get(path):
    key = os.getenv("NS_API_KEY")
    if not key:
        raise RuntimeError("NS_API_KEY is not configured")

    response = httpx.get(
        NS_BASE + path,
        headers={"Ocp-Apim-Subscription-Key": key, "Accept": "application/json"},
        timeout=15,
    )
    if response.status_code == 429:
        # Too many calls: back off as long as NS asks (at least 5 minutes).
        try:
            wait = float(response.headers.get("Retry-After", 300))
        except ValueError:
            wait = 300
        _blocked_until["t"] = time.time() + max(wait, 300)
        print(f"NS rate limit hit; pausing NS calls for {max(wait, 300):.0f} s")
    response.raise_for_status()
    return response.json()


def fetch_next(wait=True):
    """Make at most one NS call. Returns the path fetched, or None."""
    while True:
        with _call_lock:
            delay = seconds_until_call_allowed()
            if delay <= 0:
                now = time.time()
                path = _next_path(now)
                if path is None:
                    return None
                return _fetch(path, now)
        if not wait:
            return None
        # Sleep without the lock, so fetch_urgent() isn't stuck behind us.
        time.sleep(min(delay, 1))


# Someone just saved a route and is watching the page: these calls may skip
# the normal spacing, but stay far below NS's limit of 1 call per second.
URGENT_MIN_GAP = 1.5          # seconds between any two calls
URGENT_PER_MINUTE = 10        # urgent calls allowed per rolling minute
_urgent_calls = []


def fetch_urgent(path):
    """Fetch `path` right away. Returns False if the budget is used up."""
    with _call_lock:
        now = time.time()
        if _blocked_until["t"] > now:
            return False
        _urgent_calls[:] = [t for t in _urgent_calls if now - t < 60]
        if len(_urgent_calls) >= URGENT_PER_MINUTE:
            return False
        gap = _last_call() + URGENT_MIN_GAP - now
        if gap > 0:
            time.sleep(gap)
            now = time.time()
        _urgent_calls.append(now)
        _fetch(path, now)
        return True


def _fetch(path, now):
    """One real NS call (caller holds _call_lock)."""
    storage.write_json(LAST_CALL_FILE, {"at": now})
    try:
        data = _http_get(path)
        error = None
    except Exception as exc:
        print("NS API error:", path, str(exc))
        data, error = None, str(exc)

    with _lock:
        entry = _cache.setdefault(path, {"wanted": now, "max_age": DEFAULT_MAX_AGE})
        entry["attempted"] = now
        if error is None:
            entry.update(data=data, fetched=time.time())
            entry.pop("error", None)
        elif "data" not in entry:
            entry["error"] = "Could not reach NS"  # stale data is kept otherwise
    return path


def want(path, max_age=DEFAULT_MAX_AGE):
    """Ask for a path to be fetched without needing it right now."""
    try:
        ns_get(path, max_age)
    except (NeedData, RuntimeError):
        pass


def clear_cache():
    with _lock:
        _cache.clear()


# ------------------------------------------------------------ stations

def get_stations():
    data = ns_get(STATIONS_PATH, max_age=STATIONS_MAX_AGE, track=False)
    return data.get("payload", []) if isinstance(data, dict) else []


def station_names(station):
    names = station.get("namen", {})
    return [n for n in (names.get("lang"), names.get("middel"), names.get("kort")) if n]


def match_names(station):
    """Long and medium names: used to find a station in disruption text."""
    names = station.get("namen", {})
    return [n for n in (names.get("lang"), names.get("middel")) if n]


def find_station(name):
    wanted = normalize(name)

    for station in get_stations():
        if wanted in [normalize(n) for n in station_names(station)]:
            return station

    return None


def station_options():
    """Sorted long station names, for the dropdown."""
    names = {
        s["namen"]["lang"]
        for s in get_stations()
        if s.get("namen", {}).get("lang")
    }
    return sorted(names, key=str.casefold)


def suggest(name, limit=3):
    """Station names that look like what the person typed."""
    options = station_options()
    wanted = normalize(name)

    found = [o for o in options if wanted and wanted in normalize(o)][:limit]
    if found:
        return found

    by_lower = {o.casefold(): o for o in options}
    close = difflib.get_close_matches(name.casefold(), list(by_lower), n=limit, cutoff=0.5)
    return [by_lower[c] for c in close]
