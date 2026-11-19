"""Cached, rate-limited access to a train API. One `Api` per provider.

Nothing in the app calls a train API directly. Code that needs data calls
`api.get(path)`, which only reads the cache. If the answer isn't cached yet
it raises NeedData and remembers the path as wanted. The background worker
(app/scheduler.py) calls `api.fetch_next()` for every provider, which makes
at most ONE real HTTP call per `call_interval` seconds, for the wanted path
that needs it most. The time of the last call (and, for APIs with a daily
quota, the number of calls today) is stored on disk so restarts can't cause
bursts.

Every provider has its own cache, budget and 429 back-off, so a busy or
blocked provider never slows the others down.
"""
import math
import os
import threading
import time

DEFAULT_MAX_AGE = 60          # refetch when older than this (unless asked otherwise)
FORGET_AFTER = 30 * 60        # drop paths nobody asked for in this long
URGENT_DAILY_RESERVE = 0.2    # share of a daily quota urgent calls may not touch


class NeedData(Exception):
    """The answer isn't cached yet; the worker will fetch it soon."""


class Busy(Exception):
    """An answer is needed right now but the budget for that is used up."""


# How old the data read during one evaluation is. Shared by all providers:
# one device only ever reads from one provider anyway.
_used = threading.local()


def track_age(max_age=DEFAULT_MAX_AGE):
    """Start recording how old the data read by this thread is, and ask for
    data read from now on to be refreshed every `max_age` seconds."""
    _used.oldest = float("inf")
    _used.max_age = max_age


def data_age():
    """Seconds since the oldest data read since track_age() was fetched."""
    oldest = getattr(_used, "oldest", float("inf"))
    return 0 if oldest == float("inf") else time.time() - oldest


def env_int(name, default, low, high):
    try:
        value = int(os.getenv(name, default))
    except ValueError:
        value = default
    return max(low, min(high, value))


class Api:
    def __init__(self, name, label, base_url, calls_per_minute, last_call_file,
                 urgent_per_minute=10, urgent_min_gap=1.5, daily_limit=None,
                 min_block=300):
        self.name = name
        self.label = label                  # shown to people: "NS", "SNCF"
        self.base_url = base_url
        self.call_interval = 60 / calls_per_minute
        self.last_call_file = last_call_file
        # Someone just saved a route and is watching the page: these calls
        # may skip the normal spacing, within their own small budget.
        self.urgent_per_minute = urgent_per_minute
        self.urgent_min_gap = urgent_min_gap
        self.daily_limit = daily_limit      # None: no daily quota
        self.min_block = min_block          # minimum pause after a 429
        self._lock = threading.Lock()
        self._call_lock = threading.Lock()
        self._cache = {}  # path -> {"data", "fetched", "error", "attempted", "wanted", "max_age"}
        self._blocked_until = 0.0
        self._urgent_calls = []
        self._round = 0

    def new_round(self):
        """Called before this provider's devices are re-evaluated."""
        self._round += 1

    # ------------------------------------------------------- provider hooks

    def request(self, path):
        """Perform the HTTP request; returns an httpx.Response."""
        raise NotImplementedError

    def parse(self, response):
        response.raise_for_status()
        return response.json()

    def _http_get(self, path):
        response = self.request(path)
        if response.status_code == 429:
            # Too many calls: back off as long as asked (at least min_block).
            try:
                wait = float(response.headers.get("Retry-After", self.min_block))
            except ValueError:
                wait = self.min_block
            wait = max(wait, self.min_block)
            self._blocked_until = time.time() + wait
            print(f"{self.label} rate limit hit; pausing {self.label} calls for {wait:.0f} s")
        return self.parse(response)

    # ---------------------------------------------------------- cache reads

    def get(self, path, max_age=None, track=True):
        """Cached answer for `path`. `track=False`: don't count this data
        towards data_age() (for slow-changing data such as station lists)."""
        if max_age is None:
            max_age = getattr(_used, "max_age", DEFAULT_MAX_AGE)
        now = time.time()
        with self._lock:
            entry = self._cache.setdefault(path, {"attempted": 0})
            entry["wanted"] = now
            # Shortest interval asked for in this round of evaluations wins.
            if entry.get("round") != self._round:
                entry["round"] = self._round
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

    def get_now(self, path, max_age=DEFAULT_MAX_AGE):
        """Cached answer, or fetch it right away (urgent budget). Raises
        Busy when that budget is used up."""
        try:
            return self.get(path, max_age, track=False)
        except NeedData:
            pass
        if not self.fetch_urgent(path):
            raise Busy(path)
        return self.get(path, max_age, track=False)

    def want(self, path, max_age=DEFAULT_MAX_AGE):
        """Ask for a path to be fetched without needing it right now."""
        try:
            self.get(path, max_age)
        except (NeedData, RuntimeError):
            pass

    def forget(self, path):
        with self._lock:
            self._cache.pop(path, None)

    def clear_cache(self):
        with self._lock:
            self._cache.clear()

    # ------------------------------------------------------------- budget

    def _next_path(self, now):
        with self._lock:
            for path in [p for p, e in self._cache.items()
                         if now - e.get("wanted", 0) > FORGET_AFTER]:
                del self._cache[path]

            due = [
                (path, e) for path, e in self._cache.items()
                if "data" not in e or now - e["fetched"] >= e["max_age"]
            ]
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

    def _record(self):
        from . import storage  # imported late: tests set DATA_DIR first
        try:
            data = storage.read_json(self.last_call_file)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _last_call(self):
        try:
            return float(self._record()["at"])
        except (KeyError, TypeError, ValueError):
            return 0.0

    @staticmethod
    def _day(now):
        return time.strftime("%Y-%m-%d", time.gmtime(now))  # quota day, UTC

    def _calls_today(self, record, now):
        if record.get("day") != self._day(now):
            return 0
        try:
            return int(record.get("count") or 0)
        except (TypeError, ValueError):
            return 0

    def calls_left_today(self, now=None):
        if self.daily_limit is None:
            return math.inf
        now = now if now is not None else time.time()
        return self.daily_limit - self._calls_today(self._record(), now)

    def seconds_until_call_allowed(self, now=None):
        now = now if now is not None else time.time()
        wait = max(0.0, self._last_call() + self.call_interval - now, self._blocked_until - now)
        if self.calls_left_today(now) <= 0:
            wait = max(wait, 86400 - now % 86400)  # until the next UTC day
        return wait

    def _write_call(self, now):
        from . import storage
        record = self._record()
        storage.write_json(self.last_call_file, {
            "at": now,
            "day": self._day(now),
            "count": self._calls_today(record, now) + 1,
        })
        if self.daily_limit is not None and self._calls_today(record, now) + 1 == self.daily_limit:
            print(f"{self.label} daily budget of {self.daily_limit} calls used up")

    # ------------------------------------------------------------ fetching

    def fetch_next(self, wait=False):
        """Make at most one call. Returns the path fetched, or None."""
        while True:
            with self._call_lock:
                delay = self.seconds_until_call_allowed()
                if delay <= 0:
                    now = time.time()
                    path = self._next_path(now)
                    if path is None:
                        return None
                    return self._fetch(path, now)
            if not wait:
                return None
            # Sleep without the lock, so fetch_urgent() isn't stuck behind us.
            time.sleep(min(delay, 1))

    def fetch_urgent(self, path):
        """Fetch `path` right away. Returns False if the budget is used up."""
        with self._call_lock:
            now = time.time()
            # Keep part of a daily quota for background refreshes, so searches
            # and route changes can never starve the lights.
            reserve = int(self.daily_limit * URGENT_DAILY_RESERVE) if self.daily_limit else 0
            if self._blocked_until > now or self.calls_left_today(now) <= reserve:
                return False
            self._urgent_calls[:] = [t for t in self._urgent_calls if now - t < 60]
            if len(self._urgent_calls) >= self.urgent_per_minute:
                return False
            gap = self._last_call() + self.urgent_min_gap - now
            if gap > 0:
                time.sleep(gap)
                now = time.time()
            self._urgent_calls.append(now)
            self._fetch(path, now)
            return True

    def _fetch(self, path, now):
        """One real call (caller holds _call_lock)."""
        self._write_call(now)
        try:
            data = self._http_get(path)
            error = None
        except Exception as exc:
            print(f"{self.label} API error:", path, str(exc))
            data, error = None, str(exc)

        with self._lock:
            entry = self._cache.setdefault(path, {"wanted": now, "max_age": DEFAULT_MAX_AGE})
            entry["attempted"] = now
            if error is None:
                entry.update(data=data, fetched=time.time())
                entry.pop("error", None)
            elif "data" not in entry:
                entry["error"] = f"Could not reach {self.label}"  # stale data is kept otherwise
        return path
