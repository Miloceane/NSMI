import difflib
import os
import time

import httpx

from .text import normalize

NS_BASE = "https://gateway.apiportal.ns.nl/reisinformatie-api/api"

_stations = []
_stations_fetched = None


def ns_get(path):
    key = os.getenv("NS_API_KEY")
    if not key:
        raise RuntimeError("NS_API_KEY is not configured")

    response = httpx.get(
        NS_BASE + path,
        headers={
            "Ocp-Apim-Subscription-Key": key,
            "Accept": "application/json",
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def get_stations():
    global _stations, _stations_fetched

    # Station names rarely change, so cache them for one day.
    if (
        _stations
        and _stations_fetched is not None
        and time.monotonic() - _stations_fetched < 86400
    ):
        return _stations

    data = ns_get("/v2/stations?countryCodes=NL")
    _stations = data.get("payload", [])
    _stations_fetched = time.monotonic()
    return _stations


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
