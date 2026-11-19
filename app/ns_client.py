"""NS API access: station list and search on top of the shared cache.

All calls go through `api` (see app/api_cache.py): reads only hit the
cache, the background worker makes the real calls within NS's own budget
(NS_CALLS_PER_MINUTE, default 6). Because the cache is keyed by URL,
devices with the same route share their trip data and everyone shares the
disruption feed.
"""
import difflib
import os

import httpx

from . import api_cache
from .api_cache import NeedData  # noqa: F401 (re-exported for main.py)
from .text import normalize

NS_BASE = "https://gateway.apiportal.ns.nl/reisinformatie-api/api"
# NS allows 300 calls / 5 min (1 per second). We use a small, fixed share of
# that: calls are spaced at least 60 / NS_CALLS_PER_MINUTE seconds apart.
CALLS_PER_MINUTE = api_cache.env_int("NS_CALLS_PER_MINUTE", 6, 1, 30)
STATIONS_MAX_AGE = 86400      # station names: once a day
STATIONS_PATH = "/v2/stations?countryCodes=NL"


class NsApi(api_cache.Api):
    def request(self, path):
        key = os.getenv("NS_API_KEY")
        if not key:
            raise RuntimeError("NS_API_KEY is not configured")
        return httpx.get(
            self.base_url + path,
            headers={"Ocp-Apim-Subscription-Key": key, "Accept": "application/json"},
            timeout=15,
        )


api = NsApi(
    name="ns",
    label="NS",
    base_url=NS_BASE,
    calls_per_minute=CALLS_PER_MINUTE,
    last_call_file="ns_last_call.json",
)


def ns_get(path, max_age=None, track=True):
    return api.get(path, max_age, track)


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
